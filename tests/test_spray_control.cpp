// Pure timing / I/O failure checks; never opens a controller or serial port.
#include <cassert>
#include <cstdio>
#include <initializer_list>
#include "../src/flex_endo_robo/flex_endo/FlexEndoscopeDriver/spray_control.h"
using namespace flex;

struct FakeSprayIO : SprayIO {
    bool waterAvailable = true, gasAvailable = true, water = false, air = false;
    bool stopped = true, startOK = true, stopOK = true, serialOK = true, healthy = true;
    int gasStarts = 0;
    bool waterReady() const override { return waterAvailable; }
    bool gasReady() const override { return gasAvailable; }
    bool waterHealthy() const override { return healthy; }
    bool waterOn() override { water = true; stopped = false; return startOK; }
    bool waterOff() override { if (stopOK) water = false; return stopOK; }
    bool waterStopped() override { return stopped; }
    bool gas(bool on) override { if (on) ++gasStarts; if (serialOK) air = on; return serialOK; }
};

int main() {
    for (Action action : {WaterSpray, GasSpray}) {
        FakeSprayIO io;
        SprayControl c(io);
        assert(c.start(action, 100, true));
        c.tick(3099);
        assert(c.busy() && c.result == Accepted);
        assert(action == WaterSpray ? io.water : io.air);
        c.tick(3100);
        assert(!io.water && !io.air);
        io.stopped = true;
        c.tick(3101);
        assert(!c.busy() && c.result == Done);
        c.tick(10000);
        assert(!io.water && !io.air);
        assert(c.start(action, 11000, true));
        c.cancel(11500);
        io.stopped = true;
        c.tick(15000);
        assert(!c.busy() && c.result == Canceled);
        assert(!io.water && !io.air);
    }
    {
        FakeSprayIO io;
        SprayControl c(io);
        assert(c.start(WaterSpray, 100));
        c.tick(60000);
        assert(io.water && c.phase == WaterHeld); // Held actions have no timed cutoff.
        assert(!c.start(GasSpray, 60000));
        c.cancel(60001);
        assert(!io.water && !io.air && c.busy());
        io.stopped = true;
        c.tick(60002);
        assert(!c.busy() && c.result == Canceled);
        assert(c.start(GasSpray, 70000));
        assert(io.air && !io.water);
        c.cancel(70001);
        c.tick(70002);
        assert(!io.air && !c.busy());
    }
    {
        FakeSprayIO io;
        SprayControl c(io);
        assert(c.start(CleanLens, 100));
        c.tick(3099);
        assert(io.water && !io.air);
        c.tick(3100);
        assert(!io.water && !io.air && c.phase == WaitingWaterStop);
        c.tick(3500);
        assert(!io.air);
        io.stopped = true;
        c.tick(3600);
        assert(io.air && c.phase == CleaningGas);
        c.tick(6599);
        assert(io.air);
        c.tick(6600);
        assert(!io.air && !c.busy() && c.result == Done);
    }
    for (int phase = 0; phase < 3; ++phase) {
        FakeSprayIO io;
        SprayControl c(io);
        c.start(CleanLens, 100);
        if (phase >= 1) c.tick(3100);
        if (phase == 2) { io.stopped = true; c.tick(3200); }
        c.cancel(3300);
        io.stopped = true;
        c.tick(3400);
        c.tick(10000);
        assert(!c.busy() && !io.water && !io.air && c.result == Canceled);
        assert(io.gasStarts == (phase == 2 ? 1 : 0));
    }
    for (bool stopFailure : {false, true}) {
        FakeSprayIO io;
        SprayControl c(io);
        c.start(CleanLens, 100);
        io.stopOK = !stopFailure;
        c.tick(3100);
        c.tick(8100);
        assert(c.error && c.result == Refused && io.gasStarts == 0);
        io.stopOK = io.stopped = true;
        c.tick(8200);
        assert(!c.busy() && !c.start(CleanLens, 9000));
    }
    {
        FakeSprayIO io;
        SprayControl c(io);
        io.gasAvailable = false;
        assert(!c.start(GasSpray, 0) && !c.start(CleanLens, 0));
        assert(c.start(WaterSpray, 0));
    }
    {
        FakeSprayIO io;
        SprayControl c(io);
        c.start(CleanLens, 100);
        io.healthy = false;
        c.tick(3100);
        assert(c.error == -207 && io.gasStarts == 0 && !io.water);
    }
    {
        FakeSprayIO io;
        SprayControl c(io);
        io.serialOK = false;
        c.start(GasSpray, 100);
        c.tick(200);
        assert(c.busy() && c.error == -206 && c.result == Refused);
        io.serialOK = true;
        c.tick(300);
        assert(!c.busy() && !io.air);
    }
    std::puts("Spray hold/release, 3+3 timing, stop confirmation, cancellation and failure checks passed.");
}
