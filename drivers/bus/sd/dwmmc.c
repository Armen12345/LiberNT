/*
 * PROJECT:     LiberNT SD/SDIO/eMMC Bus Driver
 * LICENSE:     GPL-3.0-or-later (https://spdx.org/licenses/GPL-3.0-or-later)
 * PURPOSE:     Synopsys DesignWare mobile storage host controller backend
 * COPYRIGHT:   Copyright 2026 Ahmed ARIF <arif.ing@outlook.com>
 */

#include "dwmmc.h"

#define NDEBUG
#include <debug.h>

static ULONG
DwMmcRead(_In_ PFDO_EXTENSION FdoExtension, _In_ ULONG Register)
{
    return READ_REGISTER_ULONG((PULONG)((PUCHAR)FdoExtension->RegisterBase + Register));
}

static VOID
DwMmcWrite(_In_ PFDO_EXTENSION FdoExtension, _In_ ULONG Register, _In_ ULONG Value)
{
    WRITE_REGISTER_ULONG((PULONG)((PUCHAR)FdoExtension->RegisterBase + Register), Value);
}

static VOID
DwMmcDelayMs(_In_ ULONG Milliseconds)
{
    if (KeGetCurrentIrql() > APC_LEVEL)
    {
        KeStallExecutionProcessor(Milliseconds * 1000);
    }
    else
    {
        LARGE_INTEGER Delay;

        Delay.QuadPart = -(LONGLONG)Milliseconds * 10000;
        KeDelayExecutionThread(KernelMode, FALSE, &Delay);
    }
}

static BOOLEAN
DwMmcPollClear(_In_ PFDO_EXTENSION FdoExtension, _In_ ULONG Register, _In_ ULONG Mask, _In_ ULONG TimeoutMs)
{
    ULONG Budget = TimeoutMs * 100;

    for (;;)
    {
        if (!(DwMmcRead(FdoExtension, Register) & Mask))
            return TRUE;
        if (Budget == 0)
            return FALSE;
        KeStallExecutionProcessor(10);
        Budget--;
    }
}

static NTSTATUS
DwMmcErrorToStatus(_In_ ULONG Interrupts)
{
    if (Interrupts & DWMMC_INT_RTO)
        return STATUS_SD_CMD_TIMEOUT;
    if (Interrupts & DWMMC_INT_RCRC)
        return STATUS_SD_CMD_CRC_ERROR;
    if (Interrupts & (DWMMC_INT_DRTO | DWMMC_INT_HTO))
        return STATUS_SD_DATA_TIMEOUT;
    if (Interrupts & (DWMMC_INT_DCRC | DWMMC_INT_EBE | DWMMC_INT_SBE))
        return STATUS_SD_DATA_CRC_ERROR;
    return STATUS_SD_IO_ERROR;
}

static NTSTATUS
DwMmcUpdateClock(_In_ PFDO_EXTENSION FdoExtension)
{
    DwMmcWrite(FdoExtension, DWMMC_CMDARG, 0);
    DwMmcWrite(FdoExtension, DWMMC_CMD, DWMMC_CMD_START | DWMMC_CMD_UPD_CLK | DWMMC_CMD_PRV_DAT_WAIT);
    if (!DwMmcPollClear(FdoExtension, DWMMC_CMD, DWMMC_CMD_START, SD_CMD_TIMEOUT_MS))
    {
        DPRINT1("[DWMMC] clock update not accepted (RINTSTS=0x%08lx)\n", DwMmcRead(FdoExtension, DWMMC_RINTSTS));
        return STATUS_IO_TIMEOUT;
    }
    return STATUS_SUCCESS;
}

NTSTATUS
DwMmcSetClock(_In_ PFDO_EXTENSION FdoExtension, _In_ ULONG TargetClockKhz)
{
    ULONG BusKhz;
    ULONG Divider;
    ULONG ActualKhz;
    NTSTATUS Status;

    if (FdoExtension == NULL || FdoExtension->RegisterBase == NULL || FdoExtension->DwMmcBusHz == 0)
        return STATUS_INVALID_PARAMETER;

    DwMmcWrite(FdoExtension, DWMMC_CLKENA, 0);
    Status = DwMmcUpdateClock(FdoExtension);
    if (!NT_SUCCESS(Status))
        return Status;
    if (TargetClockKhz == 0)
    {
        InterlockedExchange(&FdoExtension->CurrentClockKhz, 0);
        return STATUS_SUCCESS;
    }

    BusKhz = FdoExtension->DwMmcBusHz / 1000;
    if (TargetClockKhz >= BusKhz)
    {
        Divider = 0;
        ActualKhz = BusKhz;
    }
    else
    {
        Divider = (BusKhz + 2 * TargetClockKhz - 1) / (2 * TargetClockKhz);
        ActualKhz = BusKhz / (2 * Divider);
    }

    DwMmcWrite(FdoExtension, DWMMC_CLKDIV, Divider);
    DwMmcWrite(FdoExtension, DWMMC_CLKSRC, 0);
    Status = DwMmcUpdateClock(FdoExtension);
    if (!NT_SUCCESS(Status))
        return Status;
    DwMmcWrite(FdoExtension, DWMMC_CLKENA, DWMMC_CLKEN_ENABLE);
    Status = DwMmcUpdateClock(FdoExtension);
    if (!NT_SUCCESS(Status))
        return Status;

    DwMmcWrite(FdoExtension, DWMMC_TMOUT, DWMMC_TMOUT_MAX);
    if (InterlockedExchange(&FdoExtension->CurrentClockKhz, (LONG)ActualKhz) != (LONG)ActualKhz)
        DPRINT1("[DWMMC] card clock %lu kHz (requested %lu kHz)\n", ActualKhz, TargetClockKhz);
    return STATUS_SUCCESS;
}

VOID
DwMmcSetBusWidth(_In_ PFDO_EXTENSION FdoExtension, _In_ UCHAR BusWidth)
{
    ULONG Type = DWMMC_CTYPE_1BIT;

    if (BusWidth == 8)
        Type = DWMMC_CTYPE_8BIT;
    else if (BusWidth == 4)
        Type = DWMMC_CTYPE_4BIT;
    DwMmcWrite(FdoExtension, DWMMC_CTYPE, Type);
    FdoExtension->CurrentBusWidth = (BusWidth == 8 || BusWidth == 4) ? BusWidth : 1;
}

static VOID
DwMmcAllocateIdmac(_Inout_ PFDO_EXTENSION FdoExtension)
{
    PHYSICAL_ADDRESS Lowest, Highest, Boundary;
    ULONG Hcon = DwMmcRead(FdoExtension, DWMMC_HCON);

    FdoExtension->DwMmcDescriptors = NULL;
    FdoExtension->DwMmcDescriptorsPhysical = 0;
    if (DWMMC_HCON_DMA_INTERFACE(Hcon) != DWMMC_HCON_DMA_INTERNAL)
        return;
    /* TODO: 64-bit IDMAC descriptors; such a host stays on polled PIO */
    if (Hcon & DWMMC_HCON_ADDRESS_64BIT)
        return;

    if (FdoExtension->DmaBuffer == NULL)
    {
        Lowest.QuadPart = 0;
        Highest.QuadPart = 0xFFFFFFFF;
        Boundary.QuadPart = 0;
        FdoExtension->DmaBuffer = MmAllocateContiguousMemorySpecifyCache(DWMMC_IDMAC_BUFFER_BYTES + PAGE_SIZE,
                                                                         Lowest, Highest, Boundary, MmNonCached);
        if (FdoExtension->DmaBuffer == NULL)
        {
            FdoExtension->DmaBufferSize = 0;
            return;
        }
        FdoExtension->DmaPhysical = MmGetPhysicalAddress(FdoExtension->DmaBuffer);
        FdoExtension->DmaBufferSize = DWMMC_IDMAC_BUFFER_BYTES;
    }

    ASSERT(FdoExtension->DmaBufferSize == DWMMC_IDMAC_BUFFER_BYTES);
    ASSERT(FdoExtension->DmaPhysical.QuadPart + DWMMC_IDMAC_BUFFER_BYTES + PAGE_SIZE <= 0x100000000LL);
    FdoExtension->DwMmcDescriptors =
        (PDWMMC_IDMAC_DESCRIPTOR)((PUCHAR)FdoExtension->DmaBuffer + DWMMC_IDMAC_BUFFER_BYTES);
    FdoExtension->DwMmcDescriptorsPhysical = FdoExtension->DmaPhysical.LowPart + DWMMC_IDMAC_BUFFER_BYTES;
}

NTSTATUS
DwMmcInitializeController(_In_ PFDO_EXTENSION FdoExtension)
{
    ULONG Version;

    if (FdoExtension->DwMmcBusHz == 0)
        return STATUS_DEVICE_CONFIGURATION_ERROR;

    FdoExtension->SpecVersion = SDHCI_SPEC_300;
    FdoExtension->HostCapabilities = SDHCI_CAP_VOLTAGE_330 |
                                     ((FdoExtension->DwMmcBusHz / 1000000) << SDHCI_CAP_BASE_CLK_SHIFT);
    if (FdoExtension->DwMmcBusWidth == 8)
        FdoExtension->HostCapabilities |= SDHCI_CAP_8BIT_SUPPORT;
    FdoExtension->HostCapabilities2 = 0;
    FdoExtension->MaxClockFrequency = FdoExtension->DwMmcBusHz / 1000;
    FdoExtension->UseAdma2 = FALSE;
    FdoExtension->UseSdma = FALSE;
    FdoExtension->CurrentBusWidth = 1;
    FdoExtension->DwMmcHighSpeed = FALSE;
    FdoExtension->DwMmcDescriptors = NULL;
    FdoExtension->DwMmcDescriptorsPhysical = 0;
    InterlockedExchange(&FdoExtension->CurrentClockKhz, 0);
    InterlockedExchange(&FdoExtension->CommandInterruptStatus, 0);

    DwMmcWrite(FdoExtension, DWMMC_PWREN, 1);
    DwMmcDelayMs(10);
    DwMmcWrite(FdoExtension, DWMMC_CTRL, DWMMC_CTRL_ALL_RESET);
    if (!DwMmcPollClear(FdoExtension, DWMMC_CTRL, DWMMC_CTRL_ALL_RESET, SD_CMD_TIMEOUT_MS))
        return STATUS_IO_TIMEOUT;

    DwMmcWrite(FdoExtension, DWMMC_RINTSTS, DWMMC_INT_ALL);
    DwMmcWrite(FdoExtension, DWMMC_INTMASK, 0);
    DwMmcWrite(FdoExtension, DWMMC_CTRL, DwMmcRead(FdoExtension, DWMMC_CTRL) & ~DWMMC_CTRL_INT_ENABLE);
    DwMmcWrite(FdoExtension, DWMMC_TMOUT, DWMMC_TMOUT_MAX);

    DwMmcAllocateIdmac(FdoExtension);
    DwMmcWrite(FdoExtension, DWMMC_BMOD, DWMMC_BMOD_SWRESET);
    DwMmcWrite(FdoExtension, DWMMC_IDINTEN, 0);
    DwMmcWrite(FdoExtension, DWMMC_IDSTS, DWMMC_IDSTS_ALL);
    /* FIXME: no high-speed while reads are polled: a full FIFO stops the card clock, which corrupts high-speed blocks */
    if (FdoExtension->DwMmcDescriptors != NULL)
        FdoExtension->HostCapabilities |= SDHCI_CAP_HIGH_SPEED;

    Version = DwMmcRead(FdoExtension, DWMMC_VERID) & 0xFFFF;
    FdoExtension->DwMmcDataOffset = (Version < DWMMC_VERSION_240A) ? DWMMC_DATA_OFFSET : DWMMC_DATA_240A_OFFSET;
    if (FdoExtension->DwMmcFifoDepth == 0)
    {
        FdoExtension->DwMmcFifoDepth =
            ((DwMmcRead(FdoExtension, DWMMC_FIFOTH) >> DWMMC_FIFOTH_RX_SHIFT) & DWMMC_FIFOTH_DEPTH_MASK) + 1;
    }
    DwMmcWrite(FdoExtension, DWMMC_FIFOTH,
               DWMMC_FIFOTH_MSIZE_8 | ((FdoExtension->DwMmcFifoDepth / 2 - 1) << DWMMC_FIFOTH_RX_SHIFT) |
               (FdoExtension->DwMmcFifoDepth / 2));
    DwMmcWrite(FdoExtension, DWMMC_CTYPE, DWMMC_CTYPE_1BIT);
    FdoExtension->DwMmcNeedInit = TRUE;

    DPRINT1("[DWMMC] version 0x%04lx, %lu Hz controller clock, FIFO %lu words, %s\n",
            Version, FdoExtension->DwMmcBusHz, FdoExtension->DwMmcFifoDepth,
            FdoExtension->DwMmcDescriptors != NULL ? "internal DMA" : "polled PIO");
    return DwMmcSetClock(FdoExtension, SD_INIT_CLOCK_KHZ);
}

NTSTATUS
DwMmcReset(_In_ PFDO_EXTENSION FdoExtension)
{
    LONG ClockKhz;

    DwMmcWrite(FdoExtension, DWMMC_CTRL, DwMmcRead(FdoExtension, DWMMC_CTRL) | DWMMC_CTRL_FIFO_RESET |
                                         DWMMC_CTRL_DMA_RESET);
    if (!DwMmcPollClear(FdoExtension, DWMMC_CTRL, DWMMC_CTRL_FIFO_RESET | DWMMC_CTRL_DMA_RESET, SD_CMD_TIMEOUT_MS) ||
        (DwMmcRead(FdoExtension, DWMMC_CMD) & DWMMC_CMD_START))
    {
        DPRINT1("[DWMMC] controller stuck; resetting the command path\n");
        DwMmcWrite(FdoExtension, DWMMC_CTRL, DwMmcRead(FdoExtension, DWMMC_CTRL) | DWMMC_CTRL_ALL_RESET);
        if (!DwMmcPollClear(FdoExtension, DWMMC_CTRL, DWMMC_CTRL_ALL_RESET, SD_CMD_TIMEOUT_MS))
            return STATUS_IO_TIMEOUT;
        ClockKhz = InterlockedCompareExchange(&FdoExtension->CurrentClockKhz, 0, 0);
        DwMmcWrite(FdoExtension, DWMMC_RINTSTS, DWMMC_INT_ALL);
        (VOID)DwMmcSetClock(FdoExtension, ClockKhz != 0 ? (ULONG)ClockKhz : SD_INIT_CLOCK_KHZ);
    }
    DwMmcWrite(FdoExtension, DWMMC_RINTSTS, DWMMC_INT_ALL);
    InterlockedExchange(&FdoExtension->CommandInterruptStatus, 0);
    return STATUS_SUCCESS;
}

static NTSTATUS
DwMmcWaitNotBusy(_In_ PFDO_EXTENSION FdoExtension, _In_ ULONG TimeoutMs)
{
    return DwMmcPollClear(FdoExtension, DWMMC_STATUS, DWMMC_STATUS_BUSY, TimeoutMs) ? STATUS_SUCCESS
                                                                                     : STATUS_IO_TIMEOUT;
}

static NTSTATUS
DwMmcIssueCommand(
    _In_ PFDO_EXTENSION FdoExtension,
    _In_ UCHAR CommandIndex,
    _In_ ULONG Argument,
    _In_ USHORT CommandFlags,
    _In_ ULONG DataFlags,
    _Out_writes_opt_(4) PULONG Response)
{
    ULONG ResponseKind = CommandFlags & SDHCI_CMD_RESP_MASK;
    ULONG Command = (CommandIndex & DWMMC_CMD_INDEX_MASK) | DWMMC_CMD_START | DWMMC_CMD_USE_HOLD_REG | DataFlags;
    ULONG Interrupts;
    ULONG Budget;

    if (CommandIndex == SDCMD_STOP_TRANSMISSION)
        Command |= DWMMC_CMD_STOP;
    else if (DataFlags & DWMMC_CMD_DAT_EXP)
        Command |= DWMMC_CMD_PRV_DAT_WAIT;
    if (ResponseKind != SDHCI_CMD_RESP_NONE)
        Command |= DWMMC_CMD_RESP_EXP;
    if (ResponseKind == SDHCI_CMD_RESP_136)
        Command |= DWMMC_CMD_RESP_LONG;
    if (CommandFlags & SDHCI_CMD_CRC_CHECK)
        Command |= DWMMC_CMD_RESP_CRC;
    if (FdoExtension->DwMmcNeedInit)
    {
        Command |= DWMMC_CMD_INIT;
        FdoExtension->DwMmcNeedInit = FALSE;
    }

    if ((Command & DWMMC_CMD_PRV_DAT_WAIT) && !NT_SUCCESS(DwMmcWaitNotBusy(FdoExtension, SD_DATA_TIMEOUT_MS)))
        DPRINT1("[DWMMC] CMD%u: card still busy, issuing anyway\n", CommandIndex);
    if (!DwMmcPollClear(FdoExtension, DWMMC_CMD, DWMMC_CMD_START, SD_CMD_TIMEOUT_MS))
    {
        (VOID)DwMmcReset(FdoExtension);
        return STATUS_IO_TIMEOUT;
    }

    DwMmcWrite(FdoExtension, DWMMC_RINTSTS, DWMMC_INT_CMD_DONE | DWMMC_INT_CMD_ERRORS);
    DwMmcWrite(FdoExtension, DWMMC_CMDARG, Argument);
    DwMmcWrite(FdoExtension, DWMMC_CMD, Command);

    Budget = SD_CMD_TIMEOUT_MS * 100;
    for (;;)
    {
        Interrupts = DwMmcRead(FdoExtension, DWMMC_RINTSTS);
        if (Interrupts & (DWMMC_INT_CMD_DONE | DWMMC_INT_HLE))
            break;
        if (Budget == 0)
        {
            DPRINT1("[DWMMC] CMD%u completion timed out (RINTSTS=0x%08lx)\n", CommandIndex, Interrupts);
            (VOID)DwMmcReset(FdoExtension);
            return STATUS_IO_TIMEOUT;
        }
        KeStallExecutionProcessor(10);
        Budget--;
    }
    DwMmcWrite(FdoExtension, DWMMC_RINTSTS, Interrupts & (DWMMC_INT_CMD_DONE | DWMMC_INT_CMD_ERRORS));

    if (!(CommandFlags & SDHCI_CMD_CRC_CHECK))
        Interrupts &= ~DWMMC_INT_RCRC;
    if (Interrupts & DWMMC_INT_CMD_ERRORS)
        return DwMmcErrorToStatus(Interrupts & DWMMC_INT_CMD_ERRORS);

    if (Response != NULL)
    {
        if (ResponseKind == SDHCI_CMD_RESP_136)
        {
            ULONG Rsp0 = DwMmcRead(FdoExtension, DWMMC_RESP0);
            ULONG Rsp1 = DwMmcRead(FdoExtension, DWMMC_RESP1);
            ULONG Rsp2 = DwMmcRead(FdoExtension, DWMMC_RESP2);
            ULONG Rsp3 = DwMmcRead(FdoExtension, DWMMC_RESP3);

            Response[0] = (Rsp1 << 24) | (Rsp0 >> 8);
            Response[1] = (Rsp2 << 24) | (Rsp1 >> 8);
            Response[2] = (Rsp3 << 24) | (Rsp2 >> 8);
            Response[3] = Rsp3 >> 8;
        }
        else if (ResponseKind != SDHCI_CMD_RESP_NONE)
        {
            Response[0] = DwMmcRead(FdoExtension, DWMMC_RESP0);
        }
    }

    if (ResponseKind == SDHCI_CMD_RESP_48_BUSY && !(DataFlags & DWMMC_CMD_DAT_EXP))
    {
        if (!NT_SUCCESS(DwMmcWaitNotBusy(FdoExtension, SD_DATA_TIMEOUT_MS)))
        {
            DPRINT1("[DWMMC] CMD%u busy wait timed out\n", CommandIndex);
            return STATUS_IO_TIMEOUT;
        }
    }
    return STATUS_SUCCESS;
}

NTSTATUS
DwMmcSendCommand(
    _In_ PFDO_EXTENSION FdoExtension,
    _In_ UCHAR CommandIndex,
    _In_ ULONG Argument,
    _In_ USHORT CommandFlags,
    _Out_opt_ PULONG Response)
{
    return DwMmcIssueCommand(FdoExtension, CommandIndex, Argument, CommandFlags, 0, Response);
}

static NTSTATUS
DwMmcTransferPio(_In_ PFDO_EXTENSION FdoExtension, _In_ BOOLEAN IsRead, _Inout_ PUCHAR Buffer, _In_ ULONG TotalBytes)
{
    PULONG Fifo = (PULONG)((PUCHAR)FdoExtension->RegisterBase + FdoExtension->DwMmcDataOffset);
    ULONG Remaining = TotalBytes / sizeof(ULONG);
    ULONG Budget = SD_DATA_TIMEOUT_MS * 100;

    while (Remaining > 0)
    {
        ULONG Interrupts = DwMmcRead(FdoExtension, DWMMC_RINTSTS);
        ULONG Count = (DwMmcRead(FdoExtension, DWMMC_STATUS) >> DWMMC_STATUS_FCNT_SHIFT) & DWMMC_STATUS_FCNT_MASK;
        ULONG Burst = IsRead ? Count : (FdoExtension->DwMmcFifoDepth - min(Count, FdoExtension->DwMmcFifoDepth));

        if (Interrupts & DWMMC_INT_DATA_ERRORS)
            return DwMmcErrorToStatus(Interrupts & DWMMC_INT_DATA_ERRORS);
        if (Burst == 0)
        {
            if (Budget == 0)
            {
                DPRINT1("[DWMMC] PIO %s stalled with %lu words left (RINTSTS=0x%08lx)\n",
                        IsRead ? "read" : "write", Remaining, Interrupts);
                return STATUS_IO_TIMEOUT;
            }
            KeStallExecutionProcessor(10);
            Budget--;
            continue;
        }

        Burst = min(Burst, Remaining);
        Remaining -= Burst;
        while (Burst-- > 0)
        {
            ULONG Value;

            if (IsRead)
            {
                Value = READ_REGISTER_ULONG(Fifo);
                RtlCopyMemory(Buffer, &Value, sizeof(Value));
            }
            else
            {
                RtlCopyMemory(&Value, Buffer, sizeof(Value));
                WRITE_REGISTER_ULONG(Fifo, Value);
            }
            Buffer += sizeof(ULONG);
        }
    }
    return STATUS_SUCCESS;
}

static VOID
DwMmcIdmacStart(_In_ PFDO_EXTENSION FdoExtension, _In_ ULONG TotalBytes)
{
    PDWMMC_IDMAC_DESCRIPTOR Descriptor = FdoExtension->DwMmcDescriptors;
    ULONG Count = (TotalBytes + DWMMC_IDMAC_CHUNK_BYTES - 1) / DWMMC_IDMAC_CHUNK_BYTES;
    ULONG Offset = 0;
    ULONG Index;

    ASSERT(Descriptor != NULL && FdoExtension->DmaBuffer != NULL);
    ASSERT(TotalBytes != 0 && TotalBytes <= FdoExtension->DmaBufferSize);
    ASSERT(Count <= DWMMC_IDMAC_DESCRIPTOR_COUNT);
    ASSERT(!(DwMmcRead(FdoExtension, DWMMC_CTRL) & (DWMMC_CTRL_DMA_ENABLE | DWMMC_CTRL_USE_IDMAC)));

    for (Index = 0; Index < Count; Index++)
    {
        ULONG Chunk = min(TotalBytes - Offset, DWMMC_IDMAC_CHUNK_BYTES);
        ULONG Control = DWMMC_DES0_OWN | DWMMC_DES0_CHAINED;

        if (Index == 0)
            Control |= DWMMC_DES0_FIRST;
        if (Index == Count - 1)
            Control |= DWMMC_DES0_LAST;
        Descriptor[Index].Size = Chunk;
        Descriptor[Index].Buffer = FdoExtension->DmaPhysical.LowPart + Offset;
        Descriptor[Index].Next = FdoExtension->DwMmcDescriptorsPhysical +
                                 ((Index + 1) % DWMMC_IDMAC_DESCRIPTOR_COUNT) * sizeof(DWMMC_IDMAC_DESCRIPTOR);
        Descriptor[Index].Control = Control;
        Offset += Chunk;
    }
    ASSERT(Offset == TotalBytes);
    KeMemoryBarrier();

    DwMmcWrite(FdoExtension, DWMMC_IDINTEN, DWMMC_IDSTS_TI | DWMMC_IDSTS_RI | DWMMC_IDSTS_FBE);
    DwMmcWrite(FdoExtension, DWMMC_IDSTS, DWMMC_IDSTS_ALL);
    DwMmcWrite(FdoExtension, DWMMC_DBADDR, FdoExtension->DwMmcDescriptorsPhysical);
    DwMmcWrite(FdoExtension, DWMMC_CTRL,
               DwMmcRead(FdoExtension, DWMMC_CTRL) | DWMMC_CTRL_DMA_ENABLE | DWMMC_CTRL_USE_IDMAC);
    DwMmcWrite(FdoExtension, DWMMC_BMOD,
               DwMmcRead(FdoExtension, DWMMC_BMOD) | DWMMC_BMOD_FIXED_BURST | DWMMC_BMOD_ENABLE);
}

static VOID
DwMmcIdmacStop(_In_ PFDO_EXTENSION FdoExtension, _In_ BOOLEAN Failed)
{
    DwMmcWrite(FdoExtension, DWMMC_CTRL,
               DwMmcRead(FdoExtension, DWMMC_CTRL) & ~(DWMMC_CTRL_DMA_ENABLE | DWMMC_CTRL_USE_IDMAC));
    DwMmcWrite(FdoExtension, DWMMC_BMOD,
               DwMmcRead(FdoExtension, DWMMC_BMOD) & ~(DWMMC_BMOD_FIXED_BURST | DWMMC_BMOD_ENABLE));
    if (Failed)
        DwMmcWrite(FdoExtension, DWMMC_BMOD, DWMMC_BMOD_SWRESET);
    DwMmcWrite(FdoExtension, DWMMC_IDSTS, DWMMC_IDSTS_ALL);
}

static NTSTATUS
DwMmcIdmacWait(_In_ PFDO_EXTENSION FdoExtension, _In_ BOOLEAN IsRead)
{
    ULONG Wanted = IsRead ? DWMMC_IDSTS_RI : DWMMC_IDSTS_TI;
    ULONG Budget = DWMMC_IDMAC_COMPLETE_TIMEOUT_MS * 100;
    ULONG Status;

    for (;;)
    {
        Status = DwMmcRead(FdoExtension, DWMMC_IDSTS);
        if (Status & DWMMC_IDSTS_FBE)
        {
            DPRINT1("[DWMMC] internal DMA bus error (IDSTS=0x%08lx)\n", Status);
            return STATUS_SD_IO_ERROR;
        }
        if (Status & Wanted)
            return STATUS_SUCCESS;
        if (Budget == 0)
        {
            DPRINT1("[DWMMC] internal DMA completion timed out (IDSTS=0x%08lx CTRL=0x%08lx BMOD=0x%08lx "
                    "STATUS=0x%08lx DSCADDR=0x%08lx BUFADDR=0x%08lx, table 0x%08lx, DES0=0x%08lx)\n",
                    Status, DwMmcRead(FdoExtension, DWMMC_CTRL), DwMmcRead(FdoExtension, DWMMC_BMOD),
                    DwMmcRead(FdoExtension, DWMMC_STATUS), DwMmcRead(FdoExtension, DWMMC_DSCADDR),
                    DwMmcRead(FdoExtension, DWMMC_BUFADDR), FdoExtension->DwMmcDescriptorsPhysical,
                    FdoExtension->DwMmcDescriptors[0].Control);
            return STATUS_IO_TIMEOUT;
        }
        KeStallExecutionProcessor(10);
        Budget--;
    }
}

static NTSTATUS
DwMmcWaitDataDone(_In_ PFDO_EXTENSION FdoExtension, _In_ BOOLEAN AutoStop)
{
    ULONG Wanted = DWMMC_INT_DATA_OVER | (AutoStop ? DWMMC_INT_ACD : 0);
    ULONG Budget = SD_DATA_TIMEOUT_MS * 100;
    ULONG Interrupts;

    for (;;)
    {
        Interrupts = DwMmcRead(FdoExtension, DWMMC_RINTSTS);
        if (Interrupts & DWMMC_INT_DATA_ERRORS)
            return DwMmcErrorToStatus(Interrupts & DWMMC_INT_DATA_ERRORS);
        if ((Interrupts & Wanted) == Wanted)
            return STATUS_SUCCESS;
        if (Budget == 0)
        {
            DPRINT1("[DWMMC] data completion timed out (RINTSTS=0x%08lx)\n", Interrupts);
            return STATUS_IO_TIMEOUT;
        }
        KeStallExecutionProcessor(10);
        Budget--;
    }
}

NTSTATUS
DwMmcExecuteDataCommand(
    _In_ PFDO_EXTENSION FdoExtension,
    _In_ UCHAR CommandIndex,
    _In_ ULONG Argument,
    _In_ USHORT CommandFlags,
    _In_ BOOLEAN IsRead,
    _In_ BOOLEAN SendStopCommand,
    _Inout_updates_bytes_(BlockSize * BlockCount) PVOID Buffer,
    _In_ ULONG BlockSize,
    _In_ ULONG BlockCount,
    _Out_opt_ PULONG Response)
{
    ULONG LocalResponse[4];
    ULONG DataFlags;
    ULONG TotalBytes = BlockSize * BlockCount;
    BOOLEAN UseIdmac = (FdoExtension->DwMmcDescriptors != NULL);
    NTSTATUS Status;

    if (Buffer == NULL || BlockSize == 0 || BlockCount == 0 || (BlockSize % sizeof(ULONG)) != 0)
        return STATUS_INVALID_PARAMETER;
    if (UseIdmac && TotalBytes > FdoExtension->DmaBufferSize)
        return STATUS_INVALID_BUFFER_SIZE;

    RtlZeroMemory(LocalResponse, sizeof(LocalResponse));
    DwMmcWrite(FdoExtension, DWMMC_CTRL, DwMmcRead(FdoExtension, DWMMC_CTRL) | DWMMC_CTRL_FIFO_RESET);
    if (!DwMmcPollClear(FdoExtension, DWMMC_CTRL, DWMMC_CTRL_FIFO_RESET, SD_CMD_TIMEOUT_MS))
        return STATUS_IO_TIMEOUT;
    DwMmcWrite(FdoExtension, DWMMC_RINTSTS, DWMMC_INT_ALL);
    DwMmcWrite(FdoExtension, DWMMC_BLKSIZ, BlockSize);
    DwMmcWrite(FdoExtension, DWMMC_BYTCNT, TotalBytes);
    if (UseIdmac)
    {
        if (!IsRead)
            RtlCopyMemory(FdoExtension->DmaBuffer, Buffer, TotalBytes);
        DwMmcIdmacStart(FdoExtension, TotalBytes);
    }

    DataFlags = DWMMC_CMD_DAT_EXP | (IsRead ? 0 : DWMMC_CMD_DAT_WR) | (SendStopCommand ? DWMMC_CMD_SEND_STOP : 0);
    Status = DwMmcIssueCommand(FdoExtension, CommandIndex, Argument, CommandFlags, DataFlags, LocalResponse);
    if (Response != NULL)
        Response[0] = LocalResponse[0];
    if (!NT_SUCCESS(Status))
    {
        if (UseIdmac)
            DwMmcIdmacStop(FdoExtension, TRUE);
        (VOID)DwMmcReset(FdoExtension);
        return Status;
    }

    if (UseIdmac)
    {
        Status = DwMmcWaitDataDone(FdoExtension, SendStopCommand);
        if (NT_SUCCESS(Status))
            Status = DwMmcIdmacWait(FdoExtension, IsRead);
        DwMmcIdmacStop(FdoExtension, (BOOLEAN)!NT_SUCCESS(Status));
        if (NT_SUCCESS(Status) && IsRead)
            RtlCopyMemory(Buffer, FdoExtension->DmaBuffer, TotalBytes);
    }
    else
    {
        Status = DwMmcTransferPio(FdoExtension, IsRead, (PUCHAR)Buffer, TotalBytes);
        if (NT_SUCCESS(Status))
            Status = DwMmcWaitDataDone(FdoExtension, SendStopCommand);
    }
    if (NT_SUCCESS(Status) && !IsRead)
        Status = DwMmcWaitNotBusy(FdoExtension, SD_DATA_TIMEOUT_MS);

    if (!NT_SUCCESS(Status))
    {
        DPRINT1("[DWMMC] CMD%u data phase failed 0x%08lx (RINTSTS=0x%08lx)\n", CommandIndex, Status,
                DwMmcRead(FdoExtension, DWMMC_RINTSTS));
        (VOID)DwMmcReset(FdoExtension);
        if (SendStopCommand)
        {
            (VOID)DwMmcSendCommand(FdoExtension, SDCMD_STOP_TRANSMISSION, 0,
                                   SDHCI_CMD_RESP_48_BUSY | SDHCI_CMD_CRC_CHECK | SDHCI_CMD_INDEX_CHECK, NULL);
        }
    }
    DwMmcWrite(FdoExtension, DWMMC_RINTSTS, DWMMC_INT_ALL);
    return Status;
}

NTSTATUS
DwMmcExecuteRequest(
    _In_ PFDO_EXTENSION FdoExtension,
    _In_ PSDCMD_DESCRIPTOR CmdDesc,
    _In_ ULONG Argument,
    _Inout_opt_ PMDL Mdl,
    _In_ ULONG DataLength,
    _In_ ULONG BlockSize,
    _In_ USHORT RequestFlags,
    _Out_opt_ PULONG Response)
{
    USHORT CommandFlags = SdBusResponseTypeToFlags(CmdDesc->ResponseType);
    PVOID DataBuffer;

    if (DataLength == 0 || Mdl == NULL || CmdDesc->TransferType == SDTT_CMD_ONLY)
    {
        if ((RequestFlags & SDRP_FLAG_WAIT_FOR_BUSY) && (CommandFlags & SDHCI_CMD_RESP_MASK) == SDHCI_CMD_RESP_48)
            CommandFlags = (USHORT)((CommandFlags & ~SDHCI_CMD_RESP_MASK) | SDHCI_CMD_RESP_48_BUSY);
        return DwMmcSendCommand(FdoExtension, (UCHAR)CmdDesc->Cmd, Argument, CommandFlags, Response);
    }

    DataBuffer = MmGetSystemAddressForMdlSafe(Mdl, NormalPagePriority);
    if (DataBuffer == NULL)
        return STATUS_INSUFFICIENT_RESOURCES;

    return DwMmcExecuteDataCommand(FdoExtension, (UCHAR)CmdDesc->Cmd, Argument, CommandFlags,
                                   CmdDesc->TransferDirection == SDTD_READ,
                                   CmdDesc->TransferType == SDTT_MULTI_BLOCK, DataBuffer, BlockSize,
                                   DataLength / BlockSize, Response);
}
