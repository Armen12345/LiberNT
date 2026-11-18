# PROJECT:     LiberNT host-native tests
# LICENSE:     GPL-3.0-or-later (https://spdx.org/licenses/GPL-3.0-or-later)
# PURPOSE:     A legacy miniport reset restores its addressing at once at PASSIVE_LEVEL and from a work item at DISPATCH_LEVEL
# COPYRIGHT:   Copyright 2026 Ahmed ARIF <arif.ing@outlook.com>
import argparse
import pathlib
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--root', required=True)
parser.add_argument('--cc', required=True)
parser.add_argument('--source', help='miniport.c to test instead of the one under --root')
args = parser.parse_args()
source = pathlib.Path(args.source or pathlib.Path(args.root) / 'drivers/network/ndis/ndis/miniport.c').read_text()

match = re.search(r'(?ms)^(?:static\nVOID\nMiniRestoreAddressing\(.*?)?^VOID\nMiniDoAddressingReset\(.*?^\}\n', source)
assert match, 'MiniDoAddressingReset not found'

fixture = r'''
#include <stdio.h>
#include <stdlib.h>
#define NTAPI
#define VOID void
#define APC_LEVEL 1
#define DISPATCH_LEVEL 2
#define OID_GEN_CURRENT_LOOKAHEAD 0x0001010F
#define DelayedWorkQueue 1
#define CHECK(x) do { if (!(x)) { printf("FAIL line %d: %s\n", __LINE__, #x); Failures++; } } while (0)
#define ASSERT(x) do { if (!(x)) { printf("ASSERT(%s) at line %d\n", #x, __LINE__); Failures++; } } while (0)
typedef unsigned char KIRQL;
typedef unsigned int ULONG, *PULONG;
typedef void *PVOID;
typedef struct _DEVICE_OBJECT { PVOID DeviceExtension; } DEVICE_OBJECT, *PDEVICE_OBJECT;
typedef VOID (*PIO_WORKITEM_ROUTINE)(PDEVICE_OBJECT, PVOID);
typedef struct _IO_WORKITEM { PDEVICE_OBJECT DeviceObject; PIO_WORKITEM_ROUTINE Routine; PVOID Context; int Queued; } IO_WORKITEM, *PIO_WORKITEM;
typedef struct { int Held; int Stopping; } EX_RUNDOWN_REF;
typedef struct
{
    struct { PDEVICE_OBJECT DeviceObject; ULONG CurrentLookahead; } NdisMiniportBlock;
    EX_RUNDOWN_REF AddressingResetRundown;
} LOGICAL_ADAPTER, *PLOGICAL_ADAPTER;

static int Failures, Sets, WaitsAboveApcLevel, Allocations, FailAllocation;
static KIRQL CurrentIrql;
static IO_WORKITEM WorkItem;

static KIRQL KeGetCurrentIrql(void) { return CurrentIrql; }
static int ExAcquireRundownProtection(EX_RUNDOWN_REF *Ref) { if (Ref->Stopping) return 0; Ref->Held++; return 1; }
static void ExReleaseRundownProtection(EX_RUNDOWN_REF *Ref) { CHECK(Ref->Held > 0); Ref->Held--; }

/* The real routine waits for the miniport when the request pends */
static int MiniSetInformation(PLOGICAL_ADAPTER Adapter, ULONG Oid, ULONG Size, PVOID Buffer, PULONG BytesRead)
{
    CHECK(Oid == OID_GEN_CURRENT_LOOKAHEAD && Size == sizeof(ULONG));
    CHECK(Buffer == &Adapter->NdisMiniportBlock.CurrentLookahead);
    if (CurrentIrql > APC_LEVEL)
        WaitsAboveApcLevel++;
    Sets++;
    *BytesRead = Size;
    return 0;
}

static PIO_WORKITEM IoAllocateWorkItem(PDEVICE_OBJECT DeviceObject)
{
    if (FailAllocation)
        return NULL;
    Allocations++;
    WorkItem.DeviceObject = DeviceObject;
    WorkItem.Queued = 0;
    return &WorkItem;
}

static void IoFreeWorkItem(PIO_WORKITEM Item) { CHECK(Item == &WorkItem); Allocations--; }

static void IoQueueWorkItem(PIO_WORKITEM Item, PIO_WORKITEM_ROUTINE Routine, int Queue, PVOID Context)
{
    CHECK(Queue == DelayedWorkQueue);
    Item->Routine = Routine;
    Item->Context = Context;
    Item->Queued = 1;
}
''' + match.group(0) + r'''
int main(void)
{
    DEVICE_OBJECT Device;
    LOGICAL_ADAPTER Adapter;

    Device.DeviceExtension = &Adapter;
    Adapter.NdisMiniportBlock.DeviceObject = &Device;
    Adapter.NdisMiniportBlock.CurrentLookahead = 1500;
    Adapter.AddressingResetRundown.Held = Adapter.AddressingResetRundown.Stopping = 0;

    /* Where waiting is allowed the lookahead is set before the reset goes on */
    CurrentIrql = 0;
    MiniDoAddressingReset(&Adapter);
    CHECK(Sets == 1 && !WorkItem.Queued && Allocations == 0);

    /* The hang timer DPC and a reset completed by the miniport's DPC are at DISPATCH_LEVEL: nothing may wait there */
    CurrentIrql = DISPATCH_LEVEL;
    MiniDoAddressingReset(&Adapter);
    CHECK(WaitsAboveApcLevel == 0);
    CHECK(Sets == 1 && WorkItem.Queued && Allocations == 1);

    /* The queued set keeps the miniport from being halted until it has run */
    CHECK(Adapter.AddressingResetRundown.Held == 1);
    CurrentIrql = 0;
    if (WorkItem.Queued)
        WorkItem.Routine(WorkItem.DeviceObject, WorkItem.Context);
    CHECK(Sets == 2 && Allocations == 0);
    CHECK(Adapter.AddressingResetRundown.Held == 0);

    /* Without a work item the reset still goes on, and still does not wait */
    FailAllocation = 1;
    CurrentIrql = DISPATCH_LEVEL;
    MiniDoAddressingReset(&Adapter);
    CHECK(WaitsAboveApcLevel == 0 && Sets == 2);
    CHECK(Adapter.AddressingResetRundown.Held == 0);

    /* Nothing is queued for a miniport that is being stopped */
    FailAllocation = 0;
    WorkItem.Queued = 0;
    Adapter.AddressingResetRundown.Stopping = 1;
    MiniDoAddressingReset(&Adapter);
    CHECK(!WorkItem.Queued && Allocations == 0 && Sets == 2);

    if (!Failures)
        printf("ndis addressing reset: set at once where it may wait, from a work item where it may not\n");
    return Failures != 0;
}
'''

with tempfile.TemporaryDirectory() as tmp:
    path = pathlib.Path(tmp)
    (path / 'reset.c').write_text(fixture)
    subprocess.run([args.cc, '-std=gnu11', '-O1', '-Wall', '-Wextra', str(path / 'reset.c'), '-o', str(path / 'reset')], check=True)
    subprocess.run([str(path / 'reset')], check=True)
