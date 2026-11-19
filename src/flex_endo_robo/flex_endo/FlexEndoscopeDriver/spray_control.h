#pragma once

#include "flex_protocol.h"

namespace flex {
// I/O is injected so timing and failure paths can be tested without hardware.
class SprayIO {
public:
    virtual ~SprayIO() = default;
    virtual bool waterReady() const = 0;
    virtual bool waterHealthy() const { return true; }
    virtual bool gasReady() const = 0;
    virtual bool waterOn() = 0;
    virtual bool waterOff() = 0;
    virtual bool waterStopped() = 0;
    virtual bool gas(bool on) = 0;
};

class SprayControl {
public:
    explicit SprayControl(SprayIO& io) : io_(io) {}
    bool busy() const { return phase != SprayIdle; }
    bool start(Action action, uint64_t now, bool timed = false) {
        if (busy() || error) return false;
        if ((action != GasSpray && !io_.waterReady()) ||
            (action != WaterSpray && !io_.gasReady())) return false;
        result = Accepted;
        timed_ = timed;
        deadline_ = now + CleanStageMs;
        if (action == GasSpray) {
            phase = GasHeld;
            if (!io_.gas(true)) fail(-206, now);
        } else {
            phase = action == CleanLens ? CleaningWater : WaterHeld;
            if (!io_.waterOn()) fail(-203, now);
        }
        return true;
    }
    void cancel(uint64_t now) {
        if (!busy() || phase == SprayStopping) return;
        result = Canceled;
        stop(now);
    }
    void tick(uint64_t now) {
        if ((phase == WaterHeld || phase == CleaningWater || phase == WaitingWaterStop) &&
            !io_.waterHealthy()) { fail(-207, now); return; }
        if (timed_ && (phase == WaterHeld || phase == GasHeld) && now >= deadline_) {
            result = Done;
            stop(now);
        }
        if (phase == CleaningWater && now >= deadline_) {
            if (!io_.waterOff()) { fail(-204, now); return; }
            phase = WaitingWaterStop;
            deadline_ = now + WaterStopTimeoutMs;
        }
        if (phase == WaitingWaterStop) {
            if (io_.waterStopped()) {
                phase = CleaningGas;
                if (!io_.gas(true)) { fail(-206, now); return; }
                deadline_ = now + CleanStageMs;
            } else if (now >= deadline_) fail(-205, now);
        } else if (phase == CleaningGas && now >= deadline_) {
            result = Done;
            stop(now);
        }
        if (phase == SprayStopping) {
            // Retry off commands after failures; never report completion while
            // an output remains unconfirmed. A failed sequence cannot resume.
            const bool waterOff = io_.waterOff();
            const bool gasOff = io_.gas(false);
            if (!waterOff) { error = -204; result = Refused; }
            if (!gasOff) { error = -206; result = Refused; }
            if (waterOff && gasOff && io_.waterStopped()) phase = SprayIdle;
            else if (now >= deadline_) { if (!error) error = -205; result = Refused; }
        }
    }
    SprayPhase phase = SprayIdle;
    int32_t error = 0;
    Result result = None;
private:
    void stop(uint64_t now) {
        phase = SprayStopping;
        deadline_ = now + WaterStopTimeoutMs;
        const bool waterOff = io_.waterOff();
        const bool gasOff = io_.gas(false);
        if (!waterOff) { error = -204; result = Refused; }
        if (!gasOff) { error = -206; result = Refused; }
    }
    void fail(int32_t code, uint64_t now) {
        error = code;
        result = Refused;
        stop(now);
    }
    SprayIO& io_;
    uint64_t deadline_ = 0;
    bool timed_ = false;
};
}
