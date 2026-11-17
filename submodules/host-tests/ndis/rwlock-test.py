# PROJECT:     LiberNT host-native tests
# LICENSE:     GPL-3.0-or-later (https://spdx.org/licenses/GPL-3.0-or-later)
# PURPOSE:     The legacy NDIS read/write lock is taken exclusively by a writer that only read it before
# COPYRIGHT:   Copyright 2026 Ahmed ARIF <arif.ing@outlook.com>
import argparse
import pathlib
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--root', required=True)
parser.add_argument('--cc', required=True)
parser.add_argument('--source', help='control.c to test instead of the one under --root')
args = parser.parse_args()
source = pathlib.Path(args.source or pathlib.Path(args.root) / 'drivers/network/ndis/ndis/control.c').read_text()

acquire = re.search(r'(?ms)^VOID\nEXPORT\nNdisAcquireReadWriteLock\(.*?^\}\n', source)
release = re.search(r'(?ms)^VOID\nEXPORT\nNdisReleaseReadWriteLock\(.*?^\}\n', source)
assert acquire and release, 'the read/write lock routines were not found'

fixture = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define EXPORT
#define IN
#define VOID void
#define TRUE 1
#define FALSE 0
#define DISPATCH_LEVEL 2
#define MAXIMUM_PROCESSORS 64
#define CHECK(x) do { if (!(x)) { printf("FAIL line %d: %s\n", __LINE__, #x); Failures++; } } while (0)
#define ASSERT(x) do { if (!(x)) { printf("ASSERT(%s) at line %d\n", #x, __LINE__); exit(1); } } while (0)
#define ASSERT_IRQL(x) ASSERT(CurrentIrql <= (x))
typedef unsigned char UCHAR, BOOLEAN, KIRQL, *PKIRQL;
typedef unsigned short USHORT;
typedef unsigned int UINT, ULONG;
typedef int LONG, *PLONG;
typedef void *PVOID;
typedef unsigned long KSPIN_LOCK, *PKSPIN_LOCK;
typedef union { UINT RefCount; UCHAR cacheLine[16]; } NDIS_RW_LOCK_REFCOUNT;
typedef struct { KSPIN_LOCK SpinLock; PVOID Context; NDIS_RW_LOCK_REFCOUNT RefCount[MAXIMUM_PROCESSORS]; } NDIS_RW_LOCK, *PNDIS_RW_LOCK;
typedef struct { USHORT LockState; KIRQL OldIrql; } LOCK_STATE, *PLOCK_STATE;

static int Failures;
static KIRQL CurrentIrql;
static PVOID CurrentThread;
static ULONG CurrentProcessor;
static signed char KeNumberProcessors = 4;

static PVOID PsGetCurrentThread(void) { return CurrentThread; }
static ULONG NdisICurrentProcessorIndex(void) { return CurrentProcessor; }
static LONG InterlockedIncrement(PLONG Value) { return ++*Value; }
static void KeRaiseIrql(KIRQL NewIrql, PKIRQL OldIrql) { *OldIrql = CurrentIrql; CurrentIrql = NewIrql; }
static void KeLowerIrql(KIRQL NewIrql) { CurrentIrql = NewIrql; }
static void KeAcquireSpinLockAtDpcLevel(PKSPIN_LOCK Lock) { ASSERT(*Lock == 0); *Lock = 1; }
static void KeReleaseSpinLockFromDpcLevel(PKSPIN_LOCK Lock) { ASSERT(*Lock == 1); *Lock = 0; }
static void KeAcquireSpinLock(PKSPIN_LOCK Lock, PKIRQL OldIrql) { KeRaiseIrql(DISPATCH_LEVEL, OldIrql); KeAcquireSpinLockAtDpcLevel(Lock); }
static void KeReleaseSpinLock(PKSPIN_LOCK Lock, KIRQL OldIrql) { KeReleaseSpinLockFromDpcLevel(Lock); KeLowerIrql(OldIrql); }
''' + acquire.group(0) + release.group(0) + r'''
static int ThreadA, ThreadB;

static void Become(PVOID Thread, ULONG Processor) { CurrentThread = Thread; CurrentProcessor = Processor; CurrentIrql = 0; }

int main(void)
{
    NDIS_RW_LOCK Lock;
    LOCK_STATE Read, Write, Inner;

    memset(&Lock, 0, sizeof(Lock));

    /* A thread that read the lock and let it go is not its exclusive owner afterwards */
    Become(&ThreadA, 0);
    NdisAcquireReadWriteLock(&Lock, FALSE, &Read);
    CHECK(Lock.RefCount[0].RefCount == 1 && Lock.SpinLock == 0);
    NdisReleaseReadWriteLock(&Lock, &Read);
    CHECK(Lock.RefCount[0].RefCount == 0 && CurrentIrql == 0);
    CHECK(Lock.Context == NULL);

    NdisAcquireReadWriteLock(&Lock, TRUE, &Write);
    CHECK(Lock.SpinLock == 1);
    CHECK(Lock.Context == &ThreadA);
    CHECK(CurrentIrql == DISPATCH_LEVEL);

    /* The writer may read, and write again, inside its own exclusive section */
    NdisAcquireReadWriteLock(&Lock, FALSE, &Inner);
    CHECK(Lock.RefCount[0].RefCount == 1 && Lock.Context == &ThreadA);
    NdisReleaseReadWriteLock(&Lock, &Inner);
    CHECK(Lock.RefCount[0].RefCount == 0 && Lock.SpinLock == 1 && Lock.Context == &ThreadA);
    NdisAcquireReadWriteLock(&Lock, TRUE, &Inner);
    NdisReleaseReadWriteLock(&Lock, &Inner);
    CHECK(Lock.SpinLock == 1 && Lock.Context == &ThreadA);

    NdisReleaseReadWriteLock(&Lock, &Write);
    CHECK(Lock.SpinLock == 0 && Lock.Context == NULL && CurrentIrql == 0);

    /* A reader on another processor leaves no owner behind either */
    Become(&ThreadB, 1);
    NdisAcquireReadWriteLock(&Lock, FALSE, &Read);
    CHECK(Lock.RefCount[1].RefCount == 1);
    NdisReleaseReadWriteLock(&Lock, &Read);
    CHECK(Lock.Context == NULL);
    NdisAcquireReadWriteLock(&Lock, TRUE, &Write);
    CHECK(Lock.SpinLock == 1 && Lock.Context == &ThreadB);
    NdisReleaseReadWriteLock(&Lock, &Write);
    CHECK(Lock.SpinLock == 0 && Lock.Context == NULL);

    if (!Failures)
        printf("ndis rwlock: a writer is exclusive whatever it read before, and may nest inside itself\n");
    return Failures != 0;
}
'''

with tempfile.TemporaryDirectory() as tmp:
    path = pathlib.Path(tmp)
    (path / 'rwlock.c').write_text(fixture)
    subprocess.run([args.cc, '-std=gnu11', '-O1', '-Wall', '-Wextra', '-Wno-unused-function', '-Wno-sign-compare',
                    str(path / 'rwlock.c'), '-o', str(path / 'rwlock')], check=True)
    subprocess.run([str(path / 'rwlock')], check=True)
