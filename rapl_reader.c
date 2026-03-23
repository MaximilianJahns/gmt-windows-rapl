/*
 * rapl_reader.c
 * 
 * Windows DLL that communicates with the ScaphandreDrv kernel driver
 * to read RAPL MSR (Model-Specific Register) values.
 *
 * Build command (Visual Studio x64 Developer Command Prompt):
 *   cl /LD rapl_reader.c /Fe:rapl_reader.dll /link /DEF:rapl_reader.def
 *
 * Or with MinGW:
 *   x86_64-w64-mingw32-gcc -shared -o rapl_reader.dll rapl_reader.c
 */

#include <windows.h>
#include <stdint.h>
#include <stdio.h>

/* ── Device path ─────────────────────────────────────────── */
#define DEVICE_PATH "\\\\.\\ScaphandreDriver"

/* ── IOCTL code (METHOD_BUFFERED, FILE_ANY_ACCESS) ────────── */
#define IOCTL_READ_MSR CTL_CODE(FILE_DEVICE_UNKNOWN, 0x800, METHOD_BUFFERED, FILE_ANY_ACCESS)

/* ── MSR Register addresses ───────────────────────────────── */

/* Intel RAPL */
#define MSR_RAPL_POWER_UNIT     0x00000606
#define MSR_PKG_ENERGY_STATUS   0x00000611
#define MSR_PKG_POWER_INFO      0x00000614
#define MSR_DRAM_ENERGY_STATUS  0x00000619
#define MSR_PP0_ENERGY_STATUS   0x00000639  /* CPU Cores */
#define MSR_PP1_ENERGY_STATUS   0x00000641  /* GPU (uncore) */
#define MSR_PLATFORM_ENERGY_STATUS 0x0000064d

/* AMD RAPL */
#define MSR_AMD_RAPL_POWER_UNIT    0xc0010299
#define MSR_AMD_CORE_ENERGY_STATUS 0xc001029a
#define MSR_AMD_PKG_ENERGY_STATUS  0xc001029b

/* ── Structs ──────────────────────────────────────────────── */

/* Must match exactly the kernel driver's struct data (8 bytes) */
#pragma pack(push, 1)
typedef struct {
    uint32_t msrRegister;
    uint32_t cpuIndex;
} msr_request_t;
#pragma pack(pop)

/* Output struct returned by read_rapl_all() */
typedef struct {
    double pkg_energy_j;        /* Package energy in Joules        */
    double dram_energy_j;       /* DRAM energy in Joules           */
    double pp0_energy_j;        /* CPU Cores energy in Joules      */
    double pp1_energy_j;        /* GPU/Uncore energy in Joules     */
    double platform_energy_j;   /* Platform energy in Joules       */
    double pkg_tdp_w;           /* Package TDP in Watts            */
    double energy_unit;         /* Energy unit (Joules per count)  */
    double power_unit;          /* Power unit                      */
    double time_unit;           /* Time unit                       */
    int    cpu_index;           /* Which CPU socket was read       */
    int    valid;               /* 1 = success, 0 = error          */
    char   error_msg[128];      /* Error description if !valid     */
} rapl_data_t;

/* ── Internal helpers ─────────────────────────────────────── */

static HANDLE open_driver(void)
{
    HANDLE h = CreateFileA(
        DEVICE_PATH,
        GENERIC_READ | GENERIC_WRITE,
        0,
        NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        NULL
    );
    return h;
}

static int read_msr(HANDLE hDev, uint32_t msr_reg, uint32_t cpu_idx, uint64_t *out)
{
    msr_request_t req = { msr_reg, cpu_idx };
    uint64_t      result = 0;
    DWORD         bytes_returned = 0;

    BOOL ok = DeviceIoControl(
        hDev,
        IOCTL_READ_MSR,
        &req,    sizeof(req),
        &result, sizeof(result),
        &bytes_returned,
        NULL
    );

    if (!ok || bytes_returned < sizeof(uint64_t)) {
        return -1;
    }
    *out = result;
    return 0;
}

/* ── Public API ───────────────────────────────────────────── */

/**
 * read_rapl_all()
 *
 * Reads all available RAPL energy counters for the given CPU socket.
 * Returns a rapl_data_t with .valid=1 on success, .valid=0 on error.
 *
 * @param cpu_index  CPU socket index (0 for first socket)
 */
__declspec(dllexport)
rapl_data_t read_rapl_all(uint32_t cpu_index)
{
    rapl_data_t out;
    memset(&out, 0, sizeof(out));
    out.cpu_index = (int)cpu_index;

    HANDLE hDev = open_driver();
    if (hDev == INVALID_HANDLE_VALUE) {
        snprintf(out.error_msg, sizeof(out.error_msg),
                 "Cannot open driver at %s. Error: %lu", DEVICE_PATH, GetLastError());
        out.valid = 0;
        return out;
    }

    uint64_t raw_unit = 0;
    if (read_msr(hDev, MSR_RAPL_POWER_UNIT, cpu_index, &raw_unit) != 0) {
        /* Try AMD unit register */
        if (read_msr(hDev, MSR_AMD_RAPL_POWER_UNIT, cpu_index, &raw_unit) != 0) {
            snprintf(out.error_msg, sizeof(out.error_msg),
                     "Cannot read RAPL_POWER_UNIT MSR.");
            CloseHandle(hDev);
            out.valid = 0;
            return out;
        }
    }

    /* Decode units from MSR_RAPL_POWER_UNIT:
     *   bits  3:0  = power unit  (1/2^n Watts)
     *   bits 12:8  = energy unit (1/2^n Joules)
     *   bits 19:16 = time unit   (1/2^n seconds)
     */
    double power_unit  = 1.0 / (double)(1ULL << ((raw_unit >>  0) & 0xF));
    double energy_unit = 1.0 / (double)(1ULL << ((raw_unit >>  8) & 0x1F));
    double time_unit   = 1.0 / (double)(1ULL << ((raw_unit >> 16) & 0xF));

    out.energy_unit = energy_unit;
    out.power_unit  = power_unit;
    out.time_unit   = time_unit;

    uint64_t raw = 0;

    /* Package energy */
    if (read_msr(hDev, MSR_PKG_ENERGY_STATUS, cpu_index, &raw) == 0)
        out.pkg_energy_j = (double)(raw & 0xFFFFFFFF) * energy_unit;

    /* DRAM energy */
    if (read_msr(hDev, MSR_DRAM_ENERGY_STATUS, cpu_index, &raw) == 0)
        out.dram_energy_j = (double)(raw & 0xFFFFFFFF) * energy_unit;

    /* PP0 (CPU Cores) energy */
    if (read_msr(hDev, MSR_PP0_ENERGY_STATUS, cpu_index, &raw) == 0)
        out.pp0_energy_j = (double)(raw & 0xFFFFFFFF) * energy_unit;

    /* PP1 (GPU/Uncore) energy */
    if (read_msr(hDev, MSR_PP1_ENERGY_STATUS, cpu_index, &raw) == 0)
        out.pp1_energy_j = (double)(raw & 0xFFFFFFFF) * energy_unit;

    /* Platform energy */
    if (read_msr(hDev, MSR_PLATFORM_ENERGY_STATUS, cpu_index, &raw) == 0)
        out.platform_energy_j = (double)(raw & 0xFFFFFFFF) * energy_unit;

    /* Package TDP from MSR_PKG_POWER_INFO bits 14:0 */
    if (read_msr(hDev, MSR_PKG_POWER_INFO, cpu_index, &raw) == 0)
        out.pkg_tdp_w = (double)(raw & 0x7FFF) * power_unit;

    CloseHandle(hDev);
    out.valid = 1;
    return out;
}

/**
 * read_single_msr()
 *
 * Low-level helper: read any allowed MSR register directly.
 * Returns the raw 64-bit value, or -1 on error.
 */
__declspec(dllexport)
int64_t read_single_msr(uint32_t msr_register, uint32_t cpu_index)
{
    HANDLE hDev = open_driver();
    if (hDev == INVALID_HANDLE_VALUE) return -1;

    uint64_t result = 0;
    int rc = read_msr(hDev, msr_register, cpu_index, &result);
    CloseHandle(hDev);

    return (rc == 0) ? (int64_t)result : -1LL;
}

/* ── DLL entry point ──────────────────────────────────────── */
BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved)
{
    (void)hinstDLL; (void)fdwReason; (void)lpvReserved;
    return TRUE;
}
