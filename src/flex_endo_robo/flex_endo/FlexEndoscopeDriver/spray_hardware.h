#pragma once

#include "spray_control.h"

namespace flex {
class SprayHardware : public SprayIO {
public:
    explicit SprayHardware(bool simulated) : simulated_(simulated) {}
    ~SprayHardware() {
        waterOff();
        if (!gas(false)) std::fputs("Gas off command failed during shutdown.\n", stderr);
        if (waterTouched_ && !simulated_) GT_AxisOff(6);
        if (serial_ != INVALID_HANDLE_VALUE) CloseHandle(serial_);
    }
    void open(bool boardReady, const std::wstring& port, double velocity) {
        velocity_ = velocity;
        if (simulated_) { waterReady_ = gasReady_ = true; return; }
        long sts = 0;
        waterReady_ = boardReady && GT_GetSts(6, &sts) == 0 && !(sts & 0x5f2);
        if (waterReady_) {
            waterTouched_ = true;
            waterReady_ = GT_AxisOn(6) == 0;
            waterReadyAt_ = GetTickCount64() + 200;
        }
        // A missing gas controller does not prevent water or Flex motion.
        const std::wstring path = L"\\\\.\\" + port;
        serial_ = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (serial_ != INVALID_HANDLE_VALUE) {
            DCB dcb{};
            dcb.DCBlength = sizeof(dcb);
            COMMTIMEOUTS timeouts{};
            timeouts.WriteTotalTimeoutConstant = 20;
            if (GetCommState(serial_, &dcb)) {
                dcb.BaudRate = CBR_9600;
                dcb.ByteSize = 8;
                dcb.StopBits = ONESTOPBIT;
                dcb.Parity = NOPARITY;
                dcb.fBinary = TRUE;
                dcb.fParity = FALSE;
                dcb.fOutxCtsFlow = dcb.fOutxDsrFlow = FALSE;
                dcb.fOutX = dcb.fInX = FALSE;
                dcb.fDtrControl = DTR_CONTROL_DISABLE;
                dcb.fRtsControl = RTS_CONTROL_DISABLE;
                gasReady_ = SetCommState(serial_, &dcb) && SetCommTimeouts(serial_, &timeouts);
            }
            if (gasReady_) gasReady_ = gas(false);
            if (!gasReady_) { CloseHandle(serial_); serial_ = INVALID_HANDLE_VALUE; }
        }
        std::printf("Spray hardware: water %s, gas %s.\n",
                    waterReady_ ? "ready" : "unavailable", gasReady_ ? "ready" : "unavailable");
    }
    bool waterReady() const override { return waterReady_ && (simulated_ || GetTickCount64() >= waterReadyAt_); }
    bool gasReady() const override { return gasReady_; }
    bool waterHealthy() const override {
        if (simulated_) return true;
        long sts = 0;
        return GT_GetSts(6, &sts) == 0 && !(sts & 0x1f2) && (sts & 0x200);
    }
    bool waterOn() override {
        if (!waterReady()) return false;
        waterTouched_ = true;
        if (simulated_) { waterRunning_ = true; return true; }
        TJogPrm jog{};
        long sts = 0;
        if (GT_GetSts(6, &sts) || (sts & 0x5f2) || GT_AxisOn(6) || GT_PrfJog(6) ||
            GT_GetJogPrm(6, &jog)) return false;
        jog.acc = jog.dec = 100;
        if (GT_SetJogPrm(6, &jog) || GT_SetVel(6, velocity_) || GT_Update(1L << 5)) return false;
        waterRunning_ = true;
        return true;
    }
    bool waterOff() override {
        if (!waterTouched_) return true;
        if (simulated_) { waterRunning_ = false; return true; }
        return GT_Stop(1L << 5, 1L << 5) == 0;
    }
    bool waterStopped() override {
        if (!waterTouched_) return true;
        if (simulated_) return !waterRunning_;
        long sts = 0;
        double velocity = 0;
        return GT_GetSts(6, &sts) == 0 && !(sts & 0x400) &&
               GT_GetPrfVel(6, &velocity) == 0 && std::abs(velocity) < 0.001;
    }
    bool gas(bool on) override {
        if (simulated_) return true;
        if (serial_ == INVALID_HANDLE_VALUE) return !on && !gasMayBeOn_;
        if (on) gasMayBeOn_ = true; // Even a failed write may have reached the device.
        const char command = on ? '1' : '0';
        DWORD written = 0;
        const bool ok = WriteFile(serial_, &command, 1, &written, nullptr) && written == 1;
        if (ok && !on) gasMayBeOn_ = false;
        return ok;
    }
private:
    bool simulated_, waterReady_ = false, gasReady_ = false;
    bool waterTouched_ = false, waterRunning_ = false, gasMayBeOn_ = false;
    double velocity_ = 30;
    uint64_t waterReadyAt_ = 0;
    HANDLE serial_ = INVALID_HANDLE_VALUE;
};
}
