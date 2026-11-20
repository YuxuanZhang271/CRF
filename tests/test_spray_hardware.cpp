// Startup/recheck regression coverage. All controller calls are fake and the
// serial open is intercepted: this executable never opens either device.
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <string>
#include "../src/flex_endo_robo/flex_endo/FlexEndoscopeDriver/gts.h"

namespace {
ULONGLONG now = 1000;
long axisStatus = 0;
short statusResult = 0, enableResult = 0;
int statusReads = 0, enables = 0, serialOpens = 0, pumpStarts = 0;

ULONGLONG fakeTickCount() { return now; }
HANDLE fakeSerialOpen(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE) {
    ++serialOpens;
    return INVALID_HANDLE_VALUE;
}
}

#define GetTickCount64 fakeTickCount
#define CreateFileW fakeSerialOpen
#include "../src/flex_endo_robo/flex_endo/FlexEndoscopeDriver/spray_hardware.h"
#undef CreateFileW
#undef GetTickCount64

GT_API GT_GetSts(short axis, long* value, short count, unsigned long*) {
    assert(axis == 6 && count == 1);
    ++statusReads;
    *value = axisStatus;
    return statusResult;
}
GT_API GT_AxisOn(short axis) { assert(axis == 6); ++enables; return enableResult; }
GT_API GT_AxisOff(short axis) { assert(axis == 6); return 0; }
GT_API GT_Stop(long mask, long option) { assert(mask == 0x20 && option == 0x20); return 0; }
GT_API GT_GetPrfVel(short axis, double* value, short count, unsigned long*) {
    assert(axis == 6 && count == 1);
    *value = 0.;
    return 0;
}
GT_API GT_PrfJog(short axis) { assert(axis == 6); return 0; }
GT_API GT_GetJogPrm(short axis, TJogPrm* value) { assert(axis == 6); *value = {}; return 0; }
GT_API GT_SetJogPrm(short axis, TJogPrm*) { assert(axis == 6); return 0; }
GT_API GT_SetVel(short axis, double) { assert(axis == 6); return 0; }
GT_API GT_Update(long mask) { assert(mask == 0x20); ++pumpStarts; return 0; }

int main() {
    {
        flex::SprayHardware water(false);
        water.open(false, L"COM4", 30.); // Flex startup fault blocks board readiness.
        assert(!water.waterReady() && statusReads == 0 && enables == 0);
        const int opened = serialOpens;
        assert(water.recheckWater(true)); // Successful explicit Flex startup recheck.
        assert(enables == 1 && serialOpens == opened);
        assert(!water.waterReady() && pumpStarts == 0);
        now += 199;
        assert(!water.waterReady());
        ++now;
        assert(water.waterReady() && pumpStarts == 0);
        assert(water.waterOn() && pumpStarts == 1); // Only fresh operator intent starts water.
        assert(water.waterOff() && water.waterStopped());
    }
    for (long fault : {0x2L, 0x10L, 0x20L, 0x40L, 0x80L, 0x100L, 0x400L}) {
        flex::SprayHardware water(false);
        axisStatus = fault;
        const int before = enables;
        assert(!water.recheckWater(true) && !water.waterReady());
        assert(enables == before); // Alarm, limit and moving checks are never bypassed.
    }
    {
        flex::SprayHardware water(false);
        axisStatus = 0;
        statusResult = 7;
        const int before = enables;
        assert(!water.recheckWater(true) && !water.waterReady() && enables == before);
        statusResult = 0;
        enableResult = 7;
        assert(!water.recheckWater(true) && !water.waterReady());
        enableResult = 0;
        assert(water.recheckWater(true));
        now += 200;
        assert(water.waterReady());
        assert(!water.recheckWater(false) && !water.waterReady());
    }
    {
        flex::SprayHardware water(true);
        const int before = statusReads;
        water.open(false, L"COM4", 30.);
        assert(water.waterReady() && water.gasReady());
        assert(water.recheckWater(true) && statusReads == before);
    }
    std::puts("Water startup recovery, settle timing, independent serial and fault interlocks passed. No hardware opened.");
}
