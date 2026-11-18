#!/usr/bin/env python3
# PROJECT:     LiberNT host-native tests
# LICENSE:     GPL-3.0-or-later (https://spdx.org/licenses/GPL-3.0-or-later)
# PURPOSE:     DesignWare MMC internal DMA: transfer path, FIFO thresholds, descriptor chain, completion and errors
# COPYRIGHT:   Copyright 2026 Ahmed ARIF <arif.ing@outlook.com>
import argparse
import pathlib
import re
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--root', required=True)
parser.add_argument('--cc', required=True)
args = parser.parse_args()
root = pathlib.Path(args.root) / 'drivers/bus/sd'

header = (root / 'dwmmc.h').read_text()
source = (root / 'dwmmc.c').read_text()

defines = ''.join(m.group(0) + '\n' for m in re.finditer(r'(?m)^#define DWMMC_\w+(?:\([^)]*\))?[ \t]+(?:.*\\\n)*.*$', header))
descriptor = re.search(r'(?ms)^typedef struct _DWMMC_IDMAC_DESCRIPTOR\n\{.*?^\} DWMMC_IDMAC_DESCRIPTOR, \*PDWMMC_IDMAC_DESCRIPTOR;\n', header)
if not descriptor:
    raise RuntimeError('DWMMC_IDMAC_DESCRIPTOR not found in dwmmc.h')


def function(name):
    match = re.search(r'(?ms)^(?:static )?(?:VOID|NTSTATUS|ULONG|BOOLEAN)\n' + name + r'\(.*?^\}\n', source)
    if not match:
        raise RuntimeError(name + ' not found in dwmmc.c')
    return match.group(0)


functions = ''.join(function(name) for name in (
    'DwMmcRead', 'DwMmcWrite', 'DwMmcPollClear', 'DwMmcErrorToStatus', 'DwMmcUseIdmac', 'DwMmcFifoThreshold',
    'DwMmcIdmacStart', 'DwMmcIdmacStop',
    'DwMmcIdmacFailed', 'DwMmcIdmacWait', 'DwMmcWaitDataDone'))

fixture = r'''
#include <stdio.h>
#include <string.h>
#include <stdint.h>
typedef unsigned char UCHAR, *PUCHAR, BOOLEAN;
typedef unsigned int ULONG, *PULONG;
typedef int LONG, NTSTATUS;
typedef long long LONGLONG;
typedef void VOID, *PVOID;
#define TRUE 1
#define FALSE 0
#define _In_
#define _Inout_
#define _Out_opt_
#define C_ASSERT(e) typedef char C_ASSERT_##__LINE__[(e) ? 1 : -1]
#define PAGE_SIZE 4096
#define NT_SUCCESS(s) ((NTSTATUS)(s) >= 0)
#define STATUS_SUCCESS ((NTSTATUS)0)
#define STATUS_IO_TIMEOUT ((NTSTATUS)0xC00000B5)
#define STATUS_SD_CMD_TIMEOUT ((NTSTATUS)0xE0040001)
#define STATUS_SD_CMD_CRC_ERROR ((NTSTATUS)0xE0040002)
#define STATUS_SD_DATA_TIMEOUT ((NTSTATUS)0xE0040003)
#define STATUS_SD_DATA_CRC_ERROR ((NTSTATUS)0xE0040004)
#define STATUS_SD_IO_ERROR ((NTSTATUS)0xE004000B)
#define SD_CMD_TIMEOUT_MS 1000
#define SD_DATA_TIMEOUT_MS 5000
#define min(a, b) ((a) < (b) ? (a) : (b))
#define DPRINT1(...) ((void)0)
static int AssertFailures;
#define ASSERT(e) do { if (!(e)) { printf("ASSERT failed: %s (line %d)\n", #e, __LINE__); AssertFailures++; } } while (0)
#define KeMemoryBarrier() __sync_synchronize()
typedef union { struct { ULONG LowPart; LONG HighPart; }; LONGLONG QuadPart; } PHYSICAL_ADDRESS;
''' + defines + descriptor.group(0) + r'''
typedef struct _FDO_EXTENSION
{
    PVOID RegisterBase;
    PVOID DmaBuffer;
    ULONG DmaBufferSize;
    PHYSICAL_ADDRESS DmaPhysical;
    PDWMMC_IDMAC_DESCRIPTOR DwMmcDescriptors;
    ULONG DwMmcDescriptorsPhysical;
} FDO_EXTENSION, *PFDO_EXTENSION;

#define TABLE_PHYSICAL 0x10000000u
#define BUFFER_PHYSICAL 0x20000000u
static UCHAR RegisterWindow[0x200];
static ULONG Registers[0x200 / 4];
static DWMMC_IDMAC_DESCRIPTOR Table[DWMMC_IDMAC_DESCRIPTOR_COUNT];
static UCHAR Bounce[DWMMC_IDMAC_BUFFER_BYTES];
static FDO_EXTENSION Fdo;

static struct
{
    BOOLEAN Running;
    BOOLEAN Suspended;
    ULONG Current;
    ULONG Completed;
    BOOLEAN IsRead;
    BOOLEAN SwResetStuck;
    BOOLEAN DmaResetWritten;
    BOOLEAN SwResetWritten;
    ULONG Steps;
} Engine;

static ULONG ModelRead(ULONG Offset)
{
    ULONG Value = Registers[Offset / 4];

    if (Offset == DWMMC_BMOD && (Value & DWMMC_BMOD_SWRESET) && !Engine.SwResetStuck)
        Registers[Offset / 4] = Value & ~DWMMC_BMOD_SWRESET;
    if (Offset == DWMMC_CTRL && (Value & (DWMMC_CTRL_DMA_RESET | DWMMC_CTRL_FIFO_RESET | DWMMC_CTRL_RESET)))
        Registers[Offset / 4] = Value & ~(DWMMC_CTRL_DMA_RESET | DWMMC_CTRL_FIFO_RESET | DWMMC_CTRL_RESET);
    return Value;
}

static void ModelWrite(ULONG Offset, ULONG Value)
{
    if (Offset == DWMMC_IDSTS || Offset == DWMMC_RINTSTS)
    {
        Registers[Offset / 4] &= ~Value;
        return;
    }
    if (Offset == DWMMC_CTRL && (Value & DWMMC_CTRL_DMA_RESET))
        Engine.DmaResetWritten = TRUE;
    if (Offset == DWMMC_BMOD && (Value & DWMMC_BMOD_SWRESET))
    {
        Engine.SwResetWritten = TRUE;
        Engine.Running = FALSE;
    }
    Registers[Offset / 4] = Value;
    if ((Registers[DWMMC_BMOD / 4] & DWMMC_BMOD_ENABLE) && (Registers[DWMMC_CTRL / 4] & DWMMC_CTRL_USE_IDMAC) &&
        !Engine.Running && Offset == DWMMC_BMOD)
    {
        Engine.Running = TRUE;
        Engine.Suspended = FALSE;
        Engine.Current = Registers[DWMMC_DBADDR / 4];
    }
}

#define READ_REGISTER_ULONG(Address) ModelRead((ULONG)((PUCHAR)(Address) - RegisterWindow))
#define WRITE_REGISTER_ULONG(Address, Value) ModelWrite((ULONG)((PUCHAR)(Address) - RegisterWindow), (Value))

static PDWMMC_IDMAC_DESCRIPTOR DescriptorAt(ULONG Physical)
{
    if (Physical < TABLE_PHYSICAL || Physical >= TABLE_PHYSICAL + sizeof(Table) ||
        (Physical - TABLE_PHYSICAL) % sizeof(DWMMC_IDMAC_DESCRIPTOR))
        return NULL;
    return &Table[(Physical - TABLE_PHYSICAL) / sizeof(DWMMC_IDMAC_DESCRIPTOR)];
}

static void EngineStep(void)
{
    PDWMMC_IDMAC_DESCRIPTOR Descriptor;

    Engine.Steps++;
    if (!Engine.Running || Engine.Suspended)
        return;
    Descriptor = DescriptorAt(Engine.Current);
    if (Descriptor == NULL || !(Descriptor->Control & DWMMC_DES0_OWN))
    {
        Registers[DWMMC_IDSTS / 4] |= DWMMC_IDSTS_DU | (1u << 9);
        Engine.Suspended = TRUE;
        return;
    }
    Descriptor->Control &= ~DWMMC_DES0_OWN;
    Engine.Completed++;
    if (!(Descriptor->Control & DWMMC_DES0_DIC))
        Registers[DWMMC_IDSTS / 4] |= (Engine.IsRead ? DWMMC_IDSTS_RI : DWMMC_IDSTS_TI) | (1u << 8);
    if (Descriptor->Control & DWMMC_DES0_LAST)
    {
        Engine.Running = FALSE;
        return;
    }
    Engine.Current = (Descriptor->Control & DWMMC_DES0_CHAINED) ? Descriptor->Next :
                     Engine.Current + sizeof(DWMMC_IDMAC_DESCRIPTOR);
}

#define KeStallExecutionProcessor(Microseconds) EngineStep()
''' + functions + r'''
static int Failures;

static void Check(int Condition, const char *Case, const char *What)
{
    static const char *LastCase, *LastWhat;

    if (Condition || (Case == LastCase && What == LastWhat))
        return;
    LastCase = Case;
    LastWhat = What;
    printf("FAIL %s: %s\n", Case, What);
    Failures++;
}

static void Reset(BOOLEAN IsRead)
{
    memset(Registers, 0, sizeof(Registers));
    memset(Table, 0, sizeof(Table));
    memset(&Engine, 0, sizeof(Engine));
    Engine.IsRead = IsRead;
    Fdo.RegisterBase = RegisterWindow;
    Fdo.DmaBuffer = Bounce;
    Fdo.DmaBufferSize = DWMMC_IDMAC_BUFFER_BYTES;
    Fdo.DmaPhysical.QuadPart = BUFFER_PHYSICAL;
    Fdo.DwMmcDescriptors = Table;
    Fdo.DwMmcDescriptorsPhysical = TABLE_PHYSICAL;
}

static void CheckChain(const char *Case, ULONG TotalBytes)
{
    ULONG Count = (TotalBytes + DWMMC_IDMAC_CHUNK_BYTES - 1) / DWMMC_IDMAC_CHUNK_BYTES;
    ULONG Sum = 0;
    ULONG Index;

    Reset(TRUE);
    Check(DwMmcIdmacStart(&Fdo, TotalBytes) == STATUS_SUCCESS, Case, "start failed");
    Engine.Running = FALSE;
    for (Index = 0; Index < Count; Index++)
    {
        ULONG Control = Table[Index].Control;
        BOOLEAN Last = (Index == Count - 1);

        Check((Control & DWMMC_DES0_OWN) != 0, Case, "descriptor not owned by the DMA");
        Check(((Control & DWMMC_DES0_FIRST) != 0) == (Index == 0), Case, "FS not only on the first descriptor");
        Check(((Control & DWMMC_DES0_LAST) != 0) == Last, Case, "LD not only on the last descriptor");
        Check(((Control & DWMMC_DES0_DIC) != 0) == !Last, Case, "DIC not on every descriptor but the last");
        Check(Table[Index].Buffer == BUFFER_PHYSICAL + Sum, Case, "buffers not contiguous in the bounce buffer");
        if (!Last)
            Check((Control & DWMMC_DES0_CHAINED) &&
                  Table[Index].Next == TABLE_PHYSICAL + (Index + 1) * sizeof(DWMMC_IDMAC_DESCRIPTOR),
                  Case, "descriptor not chained to the next one");
        Sum += Table[Index].Size;
    }
    Check(Sum == TotalBytes, Case, "descriptor sizes do not add up to the transfer");
    Check(Table[Count].Control == 0 || Count == DWMMC_IDMAC_DESCRIPTOR_COUNT, Case, "descriptor past the last one armed");
    Check(Engine.DmaResetWritten && Engine.SwResetWritten, Case, "DMA interface and engine not reset before the start");
}

static void CheckCompletion(const char *Case, ULONG TotalBytes, BOOLEAN IsRead)
{
    ULONG Count = (TotalBytes + DWMMC_IDMAC_CHUNK_BYTES - 1) / DWMMC_IDMAC_CHUNK_BYTES;
    NTSTATUS Status;

    Reset(IsRead);
    Check(DwMmcIdmacStart(&Fdo, TotalBytes) == STATUS_SUCCESS, Case, "start failed");
    Registers[DWMMC_RINTSTS / 4] |= DWMMC_INT_DATA_OVER;
    Status = DwMmcWaitDataDone(&Fdo, FALSE, TRUE);
    Check(Status == STATUS_SUCCESS, Case, "data phase failed");
    Status = DwMmcIdmacWait(&Fdo, IsRead);
    Check(Status == STATUS_SUCCESS, Case, "DMA completion failed");
    if (Engine.Completed != Count)
    {
        printf("FAIL %s: DMA completion reported after %u of %u descriptors\n", Case, Engine.Completed, Count);
        Failures++;
    }
    DwMmcIdmacStop(&Fdo);
    Check(!(Registers[DWMMC_CTRL / 4] & (DWMMC_CTRL_DMA_ENABLE | DWMMC_CTRL_USE_IDMAC)), Case, "DMA left enabled");
}

static void CheckDescriptorUnavailable(void)
{
    const char *Case = "descriptor unavailable during the data phase";
    NTSTATUS Status;

    Reset(TRUE);
    Check(DwMmcIdmacStart(&Fdo, 3 * DWMMC_IDMAC_CHUNK_BYTES) == STATUS_SUCCESS, Case, "start failed");
    Table[1].Control &= ~DWMMC_DES0_OWN;
    Status = DwMmcWaitDataDone(&Fdo, FALSE, TRUE);
    Check(Status == STATUS_SD_IO_ERROR, Case, "not reported as a DMA error");
    Check(Engine.Steps < 100, Case, "waited for the data timeout instead of stopping at the DMA error");
}

static void CheckBusError(void)
{
    const char *Case = "bus error after the data phase";

    Reset(TRUE);
    Check(DwMmcIdmacStart(&Fdo, DWMMC_IDMAC_CHUNK_BYTES) == STATUS_SUCCESS, Case, "start failed");
    Engine.Running = FALSE;
    Registers[DWMMC_IDSTS / 4] |= DWMMC_IDSTS_FBE;
    Check(DwMmcIdmacWait(&Fdo, TRUE) == STATUS_SD_IO_ERROR, Case, "bus error not reported");
}

static void CheckStuckReset(void)
{
    const char *Case = "engine reset that never finishes";

    Reset(TRUE);
    Engine.SwResetStuck = TRUE;
    Check(DwMmcIdmacStart(&Fdo, DWMMC_IDMAC_CHUNK_BYTES) == STATUS_IO_TIMEOUT, Case, "start did not time out");
    Check(!(Registers[DWMMC_BMOD / 4] & DWMMC_BMOD_ENABLE), Case, "DMA enabled while its reset was still running");
}

static void CheckPath(void)
{
    const char *Case = "transfer path";

    Reset(TRUE);
    Check(!DwMmcUseIdmac(&Fdo, 8), Case, "8-byte SCR read sent through the DMA");
    Check(!DwMmcUseIdmac(&Fdo, 12), Case, "12-byte transfer sent through the DMA");
    Check(DwMmcUseIdmac(&Fdo, 16), Case, "16-byte transfer kept off the DMA");
    Check(DwMmcUseIdmac(&Fdo, 512), Case, "one block kept off the DMA");
    Check(DwMmcUseIdmac(&Fdo, DWMMC_IDMAC_BUFFER_BYTES), Case, "full bounce buffer kept off the DMA");
    Check(!DwMmcUseIdmac(&Fdo, DWMMC_IDMAC_BUFFER_BYTES + 512), Case,
          "transfer beyond the bounce buffer sent through the DMA instead of PIO");
    Check(!DwMmcUseIdmac(&Fdo, 511 * 2048), Case, "511 x 2048-byte SDIO transfer sent through the DMA");
    Fdo.DwMmcDescriptors = NULL;
    Check(!DwMmcUseIdmac(&Fdo, 512), Case, "DMA used without a descriptor table");
}

static void CheckThreshold(ULONG FifoDepth, ULONG BlockSize, ULONG Burst, ULONG RxWatermark)
{
    static const ULONG Bursts[] = { 1, 4, 8, 16, 32, 64, 128, 256 };
    ULONG Value = DwMmcFifoThreshold(FifoDepth, BlockSize);
    ULONG Size = Value >> DWMMC_FIFOTH_MSIZE_SHIFT;

    if (Size > 7 || Bursts[Size] != Burst || ((Value >> DWMMC_FIFOTH_RX_SHIFT) & DWMMC_FIFOTH_DEPTH_MASK) != RxWatermark ||
        (Value & DWMMC_FIFOTH_DEPTH_MASK) != FifoDepth / 2)
    {
        printf("FAIL FIFO threshold for a %u-word FIFO and %u-byte blocks: FIFOTH 0x%08x, expected burst %u, "
               "RX watermark %u, TX watermark %u\n", FifoDepth, BlockSize, Value, Burst, RxWatermark, FifoDepth / 2);
        Failures++;
    }
}

int main(void)
{
    CheckPath();
    CheckThreshold(32, 512, 16, 15);
    CheckThreshold(32, 64, 16, 15);
    CheckThreshold(32, 32, 8, 7);
    CheckThreshold(32, 8, 1, 1);
    CheckThreshold(32, 12, 1, 1);
    CheckThreshold(16, 512, 8, 7);
    CheckThreshold(256, 512, 128, 127);
    CheckChain("one 512-byte block", 512);
    CheckChain("one full descriptor", DWMMC_IDMAC_CHUNK_BYTES);
    CheckChain("64 KB plus one block", 64 * 1024 + 512);
    CheckChain("full bounce buffer", DWMMC_IDMAC_BUFFER_BYTES);
    CheckCompletion("64 KB read", 64 * 1024, TRUE);
    CheckCompletion("256 KB read", DWMMC_IDMAC_BUFFER_BYTES, TRUE);
    CheckCompletion("16 KB write", 16 * 1024, FALSE);
    CheckCompletion("one-descriptor read", 512, TRUE);
    CheckDescriptorUnavailable();
    CheckBusError();
    CheckStuckReset();
    if (AssertFailures)
    {
        printf("FAIL: %d assertion(s) in the driver code\n", AssertFailures);
        Failures++;
    }
    if (Failures == 0)
        printf("dwmmc idmac: all checks passed\n");
    return Failures ? 1 : 0;
}
'''

with tempfile.TemporaryDirectory() as directory:
    c_file = pathlib.Path(directory) / 'dwmmc_idmac.c'
    binary = pathlib.Path(directory) / 'dwmmc_idmac'
    c_file.write_text(fixture)
    build = subprocess.run([args.cc, '-std=gnu11', '-Wall', '-Wno-unused-function', '-Wno-unused-local-typedefs',
                            '-o', str(binary), str(c_file)], capture_output=True, text=True)
    if build.returncode != 0:
        sys.stdout.write(build.stdout + build.stderr)
        sys.exit(1)
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    sys.stdout.write(run.stdout + run.stderr)
    sys.exit(run.returncode)
