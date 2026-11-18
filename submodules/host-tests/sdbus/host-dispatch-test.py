#!/usr/bin/env python3
# PROJECT:     LiberNT host-native tests
# LICENSE:     GPL-3.0-or-later (https://spdx.org/licenses/GPL-3.0-or-later)
# PURPOSE:     SD bus width and high-speed timing reach each host type's own state, never the SDHCI layout on other hosts
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
failures = []


def body(path, name):
    text = (root / path).read_text()
    match = re.search(r'(?ms)^(?:static\s+)?(?:VOID|NTSTATUS|BOOLEAN)\s*\n' + name + r'\(.*?^\}\n', text)
    if not match:
        raise RuntimeError(name + ' not found in ' + path)
    return match.group(0)


sdio = body('sdio.c', 'SdBusSdioEnumerateFunctions')
if 'SDHCI_HC_DATA_WIDTH' in sdio:
    failures.append('SdBusSdioEnumerateFunctions sets the bus width in the SDHCI host control register')
for width in ('1', '4'):
    if not re.search(r'SdBusSetHostBusWidth\(FdoExtension,\s*' + width + r'\)', sdio):
        failures.append('SdBusSdioEnumerateFunctions does not set a %s-bit bus through SdBusSetHostBusWidth' % width)

for path, name in (('enum.c', 'SdBusEnableSdHighSpeed'), ('enum.c', 'SdBusSetTransferClock'),
                   ('enum.c', 'SdBusEnumerateCard'), ('sdio.c', 'SdBusSdioEnumerateFunctions')):
    text = body(path, name)
    if 'SDHCI_HC_HIGH_SPEED' in text or 'DwMmcHighSpeed' in text:
        failures.append(name + ' keeps its own high-speed state instead of SdBusSetHostHighSpeed/SdBusHostIsHighSpeed')
clock = body('enum.c', 'SdBusSetTransferClock')
if re.search(r'\bSDHCI_HOST_CONTROL\b', clock):
    failures.append('SdBusSetTransferClock reads SDHCI_HOST_CONTROL on every host type')
guard = re.search(r'(?s)if \((.*?)\)\s*\{\s*USHORT HostCtrl2 = SdBusReadReg16\(FdoExtension, SDHCI_HOST_CONTROL2\)', clock)
if not guard or 'FdoExtension->HostType == SdBusHostSdhci' not in guard.group(1):
    failures.append('SdBusSetTransferClock reads SDHCI_HOST_CONTROL2 on hosts that are not SDHCI')
if 'SDHCI_CAP_HIGH_SPEED' in re.sub(r'(?s)/\*.*?\*/', '', body('sdhost.c', 'SdHostInitializeController')):
    failures.append('the polled SDHost backend advertises high speed')

fixture = r'''
#include <stdio.h>
#include <string.h>
typedef unsigned char UCHAR, BOOLEAN;
typedef unsigned short USHORT;
typedef unsigned int ULONG;
typedef void VOID;
#define TRUE 1
#define FALSE 0
#define _In_
#define SDHCI_HOST_CONTROL 0x28
#define SDHCI_HC_DATA_WIDTH_4BIT 0x02
#define SDHCI_HC_DATA_WIDTH_8BIT 0x20
#define SDHCI_HC_HIGH_SPEED 0x04
#define SDHCI_CAP_HIGH_SPEED (1u << 21)
static int AssertFailures;
#define ASSERT(e) do { if (!(e)) { printf("ASSERT failed: %s\n", #e); AssertFailures++; } } while (0)
typedef enum { SdBusHostSdhci, SdBusHostBcm2835, SdBusHostDwmmc } SDBUS_HOST_TYPE;
typedef struct _FDO_EXTENSION
{
    SDBUS_HOST_TYPE HostType;
    ULONG HostCapabilities;
    UCHAR CurrentBusWidth;
    BOOLEAN HostHighSpeed;
} FDO_EXTENSION, *PFDO_EXTENSION;
static UCHAR Register28;
static int SdhciWrites, SdHostWidth, DwMmcWidth;
static UCHAR SdBusReadReg8(PFDO_EXTENSION Fdo, ULONG Register) { (void)Fdo; return Register == SDHCI_HOST_CONTROL ? Register28 : 0; }
static void SdBusWriteReg8(PFDO_EXTENSION Fdo, ULONG Register, UCHAR Value)
{
    (void)Fdo;
    SdhciWrites++;
    if (Register == SDHCI_HOST_CONTROL)
        Register28 = Value;
}
static void SdHostSetBusWidth(PFDO_EXTENSION Fdo, UCHAR Width) { SdHostWidth = Width; Fdo->CurrentBusWidth = Width; }
static void DwMmcSetBusWidth(PFDO_EXTENSION Fdo, UCHAR Width) { DwMmcWidth = Width; Fdo->CurrentBusWidth = Width; }
''' + body('request.c', 'SdBusSetHostBusWidth') + body('request.c', 'SdBusSetHostHighSpeed') + \
    body('request.c', 'SdBusHostIsHighSpeed') + r'''
static int Failures;
static FDO_EXTENSION Fdo;

static void Check(int Condition, const char *What)
{
    if (!Condition)
    {
        printf("FAIL %s\n", What);
        Failures++;
    }
}

static void Reset(SDBUS_HOST_TYPE Type)
{
    memset(&Fdo, 0, sizeof(Fdo));
    Fdo.HostType = Type;
    Fdo.HostCapabilities = SDHCI_CAP_HIGH_SPEED;
    Register28 = 0;
    SdhciWrites = SdHostWidth = DwMmcWidth = 0;
}

int main(void)
{
    Reset(SdBusHostDwmmc);
    SdBusSetHostBusWidth(&Fdo, 4);
    Check(DwMmcWidth == 4 && Fdo.CurrentBusWidth == 4, "DesignWare 4-bit bus not set in its own register");
    Check(SdhciWrites == 0, "DesignWare bus width written to the SDHCI layout");

    Reset(SdBusHostBcm2835);
    SdBusSetHostBusWidth(&Fdo, 4);
    Check(SdHostWidth == 4 && Fdo.CurrentBusWidth == 4, "SDHost 4-bit bus not set in its own register");
    Check(SdhciWrites == 0, "SDHost bus width written to the SDHCI layout");

    Reset(SdBusHostSdhci);
    Register28 = SDHCI_HC_HIGH_SPEED | SDHCI_HC_DATA_WIDTH_8BIT;
    SdBusSetHostBusWidth(&Fdo, 4);
    Check(Register28 == (SDHCI_HC_HIGH_SPEED | SDHCI_HC_DATA_WIDTH_4BIT) && Fdo.CurrentBusWidth == 4,
          "SDHCI 4-bit bus not selected alone in the host control register");
    SdBusSetHostBusWidth(&Fdo, 1);
    Check(Register28 == SDHCI_HC_HIGH_SPEED && Fdo.CurrentBusWidth == 1, "SDHCI 1-bit bus not selected");
    Reset(SdBusHostDwmmc);
    Register28 = 0xFF;
    Check(!SdBusHostIsHighSpeed(&Fdo), "DesignWare high speed read from its command argument register");
    SdBusSetHostHighSpeed(&Fdo, TRUE);
    Check(SdBusHostIsHighSpeed(&Fdo), "DesignWare high speed not recorded");
    Check(SdhciWrites == 0, "DesignWare high speed written to its command argument register");
    SdBusSetHostHighSpeed(&Fdo, FALSE);
    Check(!SdBusHostIsHighSpeed(&Fdo), "DesignWare high speed not cleared");

    Reset(SdBusHostBcm2835);
    SdBusSetHostHighSpeed(&Fdo, TRUE);
    Check(SdBusHostIsHighSpeed(&Fdo) && SdhciWrites == 0, "SDHost high speed not recorded on its own");

    Reset(SdBusHostSdhci);
    Register28 = SDHCI_HC_DATA_WIDTH_4BIT;
    SdBusSetHostHighSpeed(&Fdo, TRUE);
    Check(Register28 == (SDHCI_HC_DATA_WIDTH_4BIT | SDHCI_HC_HIGH_SPEED), "SDHCI high-speed bit not set");
    Check(SdBusHostIsHighSpeed(&Fdo), "SDHCI high speed not read from the host control register");
    Register28 = 0;
    Check(!SdBusHostIsHighSpeed(&Fdo), "SDHCI high speed not taken from the register after a controller reset");
    Register28 = SDHCI_HC_DATA_WIDTH_4BIT | SDHCI_HC_HIGH_SPEED;
    SdBusSetHostHighSpeed(&Fdo, FALSE);
    Check(Register28 == SDHCI_HC_DATA_WIDTH_4BIT && !SdBusHostIsHighSpeed(&Fdo), "SDHCI high-speed bit not cleared");
    if (AssertFailures)
    {
        printf("FAIL %d assertion(s) in the driver code\n", AssertFailures);
        Failures++;
    }
    if (Failures == 0)
        printf("sdbus host dispatch: all checks passed\n");
    return Failures ? 1 : 0;
}
'''

for failure in failures:
    print('FAIL ' + failure)
with tempfile.TemporaryDirectory() as directory:
    c_file = pathlib.Path(directory) / 'host_dispatch.c'
    binary = pathlib.Path(directory) / 'host_dispatch'
    c_file.write_text(fixture)
    build = subprocess.run([args.cc, '-std=gnu11', '-Wall', '-Wno-unused-function', '-o', str(binary), str(c_file)],
                           capture_output=True, text=True)
    if build.returncode != 0:
        sys.stdout.write(build.stdout + build.stderr)
        sys.exit(1)
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    sys.stdout.write(run.stdout + run.stderr)
    sys.exit(1 if failures or run.returncode else 0)
