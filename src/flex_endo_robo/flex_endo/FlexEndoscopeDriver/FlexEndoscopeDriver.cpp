#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <conio.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cwchar>
#include <limits>
#include <string>

#include "flex_protocol.h"
#include "gts.h"
#include "spray_hardware.h"

namespace {
using namespace flex;

constexpr double Pi = 3.14159265358979323846;
constexpr double BendPulsesPerDegree = 68000.0 * 4.0 * Pi / 180.0;
constexpr double RollPulsesPerDegree = 123000.0 * (100.0 / 40.0) / 360.0;
constexpr double MinBendLimit = 30.0, DefaultBendLimit = 60.0, MaxBendLimit = 90.0;
constexpr double RollLimit = 180.0;
constexpr double DefaultSpeedLimit = 10.0, MinSpeedLimit = 0.1; // degrees/s
constexpr DWORD PollIntervalMs = 10;
constexpr double MinPositionDurationMs = 20.0;
constexpr double PositionBendStep = 0.18, PositionRollStep = 0.60;
constexpr double TrackSpeedMultiplier = 3.0;
constexpr double HorizonRollStep = 0.80;
// Keep these increments in sync with FlexControl's tracking commands.
constexpr double TrackBendStep = 0.32 * TrackSpeedMultiplier, TrackRollStep = 1.0 * TrackSpeedMultiplier;
constexpr double PositionTolerance = 0.15;
constexpr DWORD ConfigSettleMs = 100;
constexpr uint64_t DriveEnableSettleMs = 200;
constexpr uint64_t StepTimeoutMs = 600;
constexpr uint64_t SetupReportSettleMs = 100;
constexpr double EncoderTolerancePulses = 25.0;
// Allow a small settled profile residual: 8 pulses are <0.01 deg on roll
// and <0.002 deg on a bend axis, below the 0.15 deg pose tolerance.
constexpr double ProfileTolerancePulses = 8.0;
// Do not let unequal axis travel collapse a short roll/tendon ramp toward zero.
constexpr double MinTrapAcceleration = 0.01; // pulses/ms^2
constexpr double TrapSmoothTimeMs = 1.0;
constexpr double MaxProfileCorrectionPulses = 64.0;
constexpr unsigned MaxProfileCorrections = 2;
constexpr double BendPositionSpeed = 40.0, RollPositionSpeed = 15.0; // pulses/ms
constexpr double HorizonSpeedScale = 1.25;
constexpr double TrackSpeedScale = HorizonSpeedScale * TrackSpeedMultiplier;
// Raise the jog and return-to-zero profiles together. Keep the manual
// segment time and rates in sync with FlexControl.
constexpr double JogSpeedScale = 1.5 * TrackSpeedScale;
constexpr double JogSegmentSeconds = 0.20;
constexpr double JogBendRate = 1000.0 * BendPositionSpeed * JogSpeedScale / BendPulsesPerDegree;
constexpr double JogRollRate = 1000.0 * RollPositionSpeed * JogSpeedScale / RollPulsesPerDegree;
constexpr double StraightRollBendTolerance = 0.05;
constexpr double RollChordTolerance = 0.10; // degrees of bend
constexpr long MotionMask = 0x1f, DriveEnabled = 0x200, ProfileMoving = 0x400;
// Axis status bits for drive alarm, following error, limits, and I/O stops.
constexpr long AxisFaultBits = 0x1f2;
volatile LONG quitting = 0;

BOOL WINAPI consoleEvent(DWORD event) {
    if (event != CTRL_C_EVENT && event != CTRL_BREAK_EVENT && event != CTRL_CLOSE_EVENT)
        return FALSE;
    InterlockedExchange(&quitting, 1);
    return TRUE;
}

std::wstring objectName(const wchar_t* suffix) {
    return std::wstring(Prefix) + suffix;
}

bool applicationRunning() {
    const HANDLE application = OpenMutexW(SYNCHRONIZE, FALSE, ApplicationMutex);
    if (!application) return GetLastError() != ERROR_FILE_NOT_FOUND;
    CloseHandle(application);
    return true;
}

template<class T> class Mapping {
public:
    ~Mapping() {
        if (view_) UnmapViewOfFile(view_);
        if (file_) CloseHandle(file_);
        if (mutex_) CloseHandle(mutex_);
    }

    bool open(const wchar_t* suffix) {
        const auto name = objectName(suffix);
        mutex_ = CreateMutexW(nullptr, FALSE, (name + L"Mutex").c_str());
        if (!mutex_) return false;
        file_ = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
                                  0, sizeof(T), name.c_str());
        if (!file_) return false;
        view_ = static_cast<T*>(MapViewOfFile(file_, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(T)));
        T blank{};
        return view_ && write(blank);
    }

    bool read(T& value, bool* busy = nullptr) { return copy(value, false, busy); }
    bool write(T value) { return copy(value, true); }

private:
    bool copy(T& value, bool writing, bool* busy = nullptr) {
        const DWORD result = WaitForSingleObject(mutex_, 2);
        if (busy) *busy = result == WAIT_TIMEOUT;
        if (result != WAIT_OBJECT_0) {
            if (result == WAIT_ABANDONED) {
                *view_ = T{};
                ReleaseMutex(mutex_);
            }
            return false;
        }
        if (writing) *view_ = value;
        else value = *view_;
        return ReleaseMutex(mutex_) != FALSE;
    }

    HANDLE file_ = nullptr, mutex_ = nullptr;
    T* view_ = nullptr;
};

struct Pose { double x = 0, y = 0, roll = 0; };
using Axes = std::array<double, 5>;

class Motion {
public:
    explicit Motion(bool simulated) : simulated_(simulated) {}
    ~Motion() {
        if (!opened_) return;
        stop();
        for (short axis = 1; axis <= 5; ++axis)
            if (enabled_ & (1L << (axis - 1))) check(GT_AxisOff(axis), "GT_AxisOff", axis);
        check(GT_Close(), "GT_Close");
    }

    bool open() {
        if (simulated_) return true;
        if (!check(GT_Open(), "GT_Open")) return false;
        opened_ = true;
        if (!check(GT_Reset(), "GT_Reset")) return false;
        char config[] = "GTS800.cfg";
        if (!check(GT_LoadConfig(config), "GT_LoadConfig(GTS800.cfg)")) return false;
        char path[MAX_PATH]{};
        const DWORD length = GetFullPathNameA(config, MAX_PATH, path, nullptr);
        if (length && length < MAX_PATH) std::printf("Loaded GTS config: %s\n", path);
        // Let the configured inputs settle before the startup status clear.
        Sleep(ConfigSettleMs);
        if (!check(GT_ClrSts(1, 8), "GT_ClrSts")) return false;
        const bool valid = sample();
        startupFault_ = !valid && hasAxisInputFault();
        return valid;
    }

    bool canRecheckStartup() const { return startupFault_ && !enabled_; }

    bool recheckStartup() {
        if (!canRecheckStartup()) return false;
        fault_ = 0;
        faultAxisStatus_ = 0;
        const bool valid = check(GT_ClrSts(1, 8), "GT_ClrSts(startup recheck)") && sample();
        startupFault_ = !valid && hasAxisInputFault();
        if (valid) std::puts("GTS startup inputs are clear.");
        return valid;
    }

    bool sample(uint64_t now = GetTickCount64()) {
        if (fault_) return false;
        if (simulated_) {
            if (simulatedStepMs_ > 0.0) {
                const double fraction = std::min(1.0, (now - simulatedStepAt_) / simulatedStepMs_);
                for (size_t i = 0; i < profile_.size(); ++i) {
                    profile_[i] = stepProfile_[i] + fraction * (stepTarget_[i] - stepProfile_[i]);
                }
                if (fraction == 1.0) simulatedStepMs_ = 0.0;
            }
            if (jogActive_) {
                for (size_t i = 0; i < profile_.size(); ++i) {
                    profile_[i] += simulatedJogVelocity_[i] * (now - simulatedJogAt_);
                }
                simulatedJogAt_ = now;
            }
            encoder_ = profile_;
            return true;
        }
        const Axes previous = encoder_;
        if (!check(GT_GetEncPos(1, encoder_.data(), 5), "GT_GetEncPos") ||
            !check(GT_GetPrfPos(1, profile_.data(), 5), "GT_GetPrfPos")) return false;
        std::array<long, 5> sts{};
        if (!check(GT_GetSts(1, sts.data(), 5), "GT_GetSts")) return false;
        moving_ = false;
        for (size_t i = 0; i < sts.size(); ++i) {
            if (!std::isfinite(encoder_[i]) || !std::isfinite(profile_[i]) ||
                encoder_[i] < std::numeric_limits<long>::min() ||
                encoder_[i] > std::numeric_limits<long>::max() ||
                (sts[i] & AxisFaultBits)) {
                fault_ = -100 - static_cast<int>(i);
                faultAxisStatus_ = static_cast<uint32_t>(sts[i]);
                std::fprintf(stderr, "GTS axis %zu status 0x%lX (profile %.0f, encoder %.0f)\n",
                             i + 1, static_cast<unsigned long>(sts[i]), profile_[i], encoder_[i]);
                printFaultSnapshot(sts);
                return false;
            }
            if ((enabled_ & (1L << i)) && !(sts[i] & DriveEnabled)) {
                fault_ = -114;
                std::fprintf(stderr, "GTS axis %zu did not report drive enable (status 0x%lX).\n",
                             i + 1, static_cast<unsigned long>(sts[i]));
                printFaultSnapshot(sts);
                return false;
            }
            moving_ |= (sts[i] & ProfileMoving) != 0 ||
                       (sampled_ && std::abs(encoder_[i] - previous[i]) > 2.0);
        }
        sampled_ = true;
        if (setupJogMask_ && GetTickCount64() >= stepDeadline_) {
            if (!setupJogHasFeedback(setupJogMask_)) {
                setupFeedbackMismatch_ = true;
                std::fputs("Flex setup stopped: encoder travel did not match the velocity jog.\n", stderr);
                return stop();
            }
            setupJogMask_ = 0;
        }
        if (stepMask_) {
            bool waiting = moving_;
            for (size_t i = 0; i < encoder_.size(); ++i) {
                if (!(stepMask_ & (1L << i))) continue;
                const double profileError = profile_[i] - stepTarget_[i];
                const bool profileReached = std::abs(profileError) <= ProfileTolerancePulses;
                const double error = std::abs((encoder_[i] - stepEncoder_[i]) -
                                              (stepTarget_[i] - stepProfile_[i]));
                if (profileReached && (!verifyEncoder_ || error <= EncoderTolerancePulses)) continue;
                waiting = true;
                if (now >= stepDeadline_) {
                    if (correctPlannedResidual()) return true;
                    if (fault_) return false;
                    fault_ = profileReached ? -113 : -112;
                    std::fprintf(stderr,
                                 "GTS axis %zu: %s. Target %.0f, profile %.0f "
                                 "(error %+.3f pulses, tolerance %.0f), encoder %.0f (was %.0f), "
                                 "feedback %s, status 0x%lX.\n",
                                 i + 1, profileReached ? "encoder did not follow" : "profile did not reach target",
                                 stepTarget_[i], profile_[i], profileError, ProfileTolerancePulses,
                                 encoder_[i], stepEncoder_[i], verifyEncoder_ ? "encoder" : "planned",
                                 static_cast<unsigned long>(sts[i]));
                    return false;
                }
            }
            if (waiting && now >= stepDeadline_) {
                // Being near the target must not bypass the motion timeout.
                fault_ = -112;
                std::fputs("GTS profile motion remained active past its completion deadline.\n", stderr);
                return false;
            }
            if (!waiting) stepMask_ = 0;
        }
        if (setupReportAt_ && !moving_ && GetTickCount64() >= setupReportAt_)
            reportSetupTravel(sts);
        return true;
    }

    bool stop() {
        stepMask_ = 0;
        stepCorrections_ = 0;
        simulatedStepMs_ = 0.0;
        setupJogMask_ = 0;
        jogActive_ = false;
        if (setupReportMask_ && !setupReportAt_)
            setupReportAt_ = GetTickCount64() + (verifyReportFeedback_ ? SetupReportSettleMs : 0);
        return !opened_ || !enabled_ || check(GT_Stop(enabled_, enabled_), "GT_Stop");
    }

    bool enable() {
        if (fault_) return false;
        if (simulated_ || enabled_ == MotionMask) return true;
        if (moving()) return true;
        for (short axis = 1; axis <= 5; ++axis) {
            const size_t index = axis - 1;
            if (!check(GT_AxisOn(axis), "GT_AxisOn", axis)) return false;
            enabled_ |= 1L << index;
        }
        enableReadyAt_ = GetTickCount64() + DriveEnableSettleMs;
        std::printf("GTS drive enable requested. Waiting %llu ms before the first move.\n",
                    static_cast<unsigned long long>(DriveEnableSettleMs));
        return sample();
    }

    bool ready() const {
        return simulated_ || (enabled_ == MotionMask && GetTickCount64() >= enableReadyAt_);
    }

    double speedLimit() const { return speedLimit_; }
    void setSpeedLimit(double limit) { speedLimit_ = limit; }

    bool position(Pose next, const Axes& reference, bool planned, double speedScale = 1.0) {
        const Axes target{
            reference[0] - next.x * BendPulsesPerDegree,
            reference[1] - next.y * BendPulsesPerDegree,
            reference[2] + next.x * BendPulsesPerDegree,
            reference[3] + next.y * BendPulsesPerDegree,
            reference[4] + next.roll * RollPulsesPerDegree,
        };
        return positionAxes(target, !planned, false, speedScale);
    }

    bool returnToZero(const Axes& reference, bool planned) {
        return positionAxes(reference, !planned, true, JogSpeedScale);
    }

private:
    bool hasAxisInputFault() const {
        return fault_ >= -104 && fault_ <= -100 && (faultAxisStatus_ & AxisFaultBits);
    }

    bool positionAxes(const Axes& target, bool verifyEncoder, bool exact, double speedScale = 1.0,
                      bool correction = false) {
        if (!correction) stepCorrections_ = 0;
        const Axes& positionFeedback = verifyEncoder ? encoder_ : profile_;
        Axes command{};
        double durationMs = MinPositionDurationMs;
        double maxDistance = 0.0;
        for (size_t i = 0; i < target.size(); ++i) {
            const double error = target[i] - positionFeedback[i];
            const double position = profile_[i] + error;
            if (position < std::numeric_limits<long>::min() ||
                position > std::numeric_limits<long>::max()) { fault_ = -111; return false; }
            command[i] = !exact && std::abs(error) < 1.0 ? profile_[i] :
                         simulated_ ? position : std::lround(position);
            const double speed = (i == 4 ? RollPositionSpeed : BendPositionSpeed) * speedScale;
            const double distance = std::abs(command[i] - profile_[i]);
            maxDistance = std::max(maxDistance, distance);
            durationMs = std::max(durationMs, distance / speed);
        }
        if (maxDistance < 0.5) return true;
        const double dx = std::max(std::abs(command[0] - profile_[0]), std::abs(command[2] - profile_[2]))
                          / BendPulsesPerDegree;
        const double dy = std::max(std::abs(command[1] - profile_[1]), std::abs(command[3] - profile_[3]))
                          / BendPulsesPerDegree;
        const double roll = (command[4] - profile_[4]) / RollPulsesPerDegree;
        durationMs = std::max(durationMs, 1000.0 * std::hypot(dx, dy, roll) / speedLimit_);
        if (simulated_) {
            if (command != profile_) {
                stepProfile_ = profile_;
                stepTarget_ = command;
                simulatedStepAt_ = GetTickCount64();
                simulatedStepMs_ = durationMs;
            }
            return true;
        }
        const double acceleration = 0.5 * std::max(1.0, speedScale / HorizonSpeedScale);
        Axes rampAcceleration{};
        double rampAllowanceMs = 0.0;
        for (size_t i = 0; i < target.size(); ++i) {
            const double distance = std::abs(command[i] - profile_[i]);
            if (distance < 0.5) continue;
            rampAcceleration[i] = std::max(MinTrapAcceleration, acceleration * distance / maxDistance);
            rampAllowanceMs = std::max(rampAllowanceMs, distance / (durationMs * rampAcceleration[i]));
        }
        // durationMs above bounds cruise speed. Include acceleration/deceleration
        // and smoothing, then solve each trapezoid for the same completion time.
        durationMs += rampAllowanceMs + 2 * TrapSmoothTimeMs;
        if (!trapMode_) {
            for (short axis = 1; axis <= 5; ++axis) {
                if (!check(GT_PrfTrap(axis), "GT_PrfTrap", axis)) return false;
            }
            trapMode_ = true;
        }
        long mask = 0;
        for (short axis = 1; axis <= 5; ++axis) {
            const size_t i = axis - 1;
            const double profileDistance = std::abs(command[i] - profile_[i]);
            if (profileDistance < 0.5) continue;
            const double axisAcceleration = rampAcceleration[i];
            const double travelTime = durationMs - 2 * TrapSmoothTimeMs;
            const double discriminant = std::max(0.0, travelTime * travelTime -
                                                 4 * profileDistance / axisAcceleration);
            const double velocity = 2 * profileDistance / (travelTime + std::sqrt(discriminant));
            TTrapPrm parameters{axisAcceleration, axisAcceleration, 0.0, 1};
            const short result = GT_SetTrapPrm(axis, &parameters);
            if (result) {
                std::fprintf(stderr, "GTS axis %d trap rejected: target %.0f, start %.3f, "
                             "distance %.3f, acc %.9g, dec %.9g, velocity %.9g, duration %.3f ms.\n",
                             axis, command[i], profile_[i], profileDistance,
                             parameters.acc, parameters.dec, velocity, durationMs);
            }
            if (!check(result, "GT_SetTrapPrm", axis) ||
                !check(GT_SetPos(axis, static_cast<long>(command[i])), "GT_SetPos", axis) ||
                !check(GT_SetVel(axis, velocity), "GT_SetVel", axis)) return false;
            stepTarget_[i] = command[i];
            mask |= 1L << i;
        }
        if (!mask) return true;
        if (!check(GT_Update(mask), "GT_Update")) return false;
        stepMask_ = mask;
        stepProfile_ = profile_;
        stepEncoder_ = encoder_;
        verifyEncoder_ = verifyEncoder;
        stepSpeedScale_ = speedScale;
        stepDeadline_ = GetTickCount64() + static_cast<uint64_t>(std::ceil(durationMs)) + StepTimeoutMs;
        return true;
    }

    bool correctPlannedResidual() {
        if (verifyEncoder_ || moving_ || stepCorrections_ >= MaxProfileCorrections) return false;
        Axes target = profile_;
        bool needed = false;
        for (size_t i = 0; i < target.size(); ++i) {
            if (!(stepMask_ & (1L << i))) continue;
            const double error = std::abs(stepTarget_[i] - profile_[i]);
            if (error > MaxProfileCorrectionPulses) return false;
            if (error > ProfileTolerancePulses) {
                target[i] = stepTarget_[i];
                needed = true;
            }
        }
        if (!needed) return false;
        ++stepCorrections_;
        for (size_t i = 0; i < target.size(); ++i) {
            if (target[i] == profile_[i]) continue;
            std::fprintf(stderr, "GTS axis %zu: correcting stopped planned residual %+.3f pulses "
                         "toward %.0f (attempt %u/%u).\n", i + 1, target[i] - profile_[i],
                         target[i], stepCorrections_, MaxProfileCorrections);
        }
        return positionAxes(target, false, true, std::min(1.0, stepSpeedScale_), true);
    }

public:
    bool rollBy(double pulses, bool verifyFeedback) {
        const Axes& feedback = verifyFeedback ? encoder_ : profile_;
        Axes target = feedback;
        target[4] += pulses;
        return positionAxes(target, verifyFeedback, false, JogSpeedScale);
    }

    bool setupJog(int x, int y, bool verifyFeedback) {
        if (jogActive_) return true;
        const double bendSpeed = std::min(BendPositionSpeed * JogSpeedScale,
            speedLimit_ * BendPulsesPerDegree / (1000.0 * std::hypot(x, y)));
        const Axes velocity{-x * bendSpeed, -y * bendSpeed, x * bendSpeed, y * bendSpeed,
                            0.0};
        if (simulated_) {
            simulatedJogVelocity_ = velocity;
            simulatedJogAt_ = GetTickCount64();
            jogActive_ = true;
            return true;
        }
        if (moving()) return true;
        trapMode_ = false;
        long mask = 0;
        for (short axis = 1; axis <= 5; ++axis) {
            const size_t i = axis - 1;
            if (!velocity[i]) continue;
            TJogPrm ramp{};
            if (!check(GT_PrfJog(axis), "GT_PrfJog", axis) ||
                !check(GT_GetJogPrm(axis, &ramp), "GT_GetJogPrm", axis)) return false;
            ramp.acc = ramp.dec = 100;
            if (!check(GT_SetJogPrm(axis, &ramp), "GT_SetJogPrm", axis) ||
                !check(GT_SetVel(axis, velocity[i]), "GT_SetVel", axis)) return false;
            mask |= 1L << i;
        }
        if (!mask || !check(GT_Update(mask), "GT_Update(jog)")) return false;
        recordSetupMove(mask, verifyFeedback);
        jogActive_ = true;
        setupJogMask_ = verifyFeedback ? mask : 0;
        stepDeadline_ = GetTickCount64() + StepTimeoutMs;
        std::printf("Web UI jog started (axis mask 0x%lX).\n", mask);
        return true;
    }

    const Axes& encoder() const { return encoder_; }
    const Axes& profile() const { return profile_; }
    bool moving() const {
        return moving_ || stepMask_ || simulatedStepMs_ > 0.0 || (simulated_ && jogActive_) || setupReportPending();
    }
    bool setupReportPending() const { return setupReportAt_ != 0; }
    bool setupFeedbackMismatch() const { return setupFeedbackMismatch_; }
    int fault() const { return fault_; }
    uint32_t faultAxisStatus() const { return faultAxisStatus_; }

private:
    void recordSetupMove(long mask, bool verifyFeedback) {
        verifyReportFeedback_ = verifyFeedback;
        for (size_t i = 0; i < profile_.size(); ++i) {
            const long bit = 1L << i;
            if (!(mask & bit)) continue;
            if (!(setupReportMask_ & bit)) {
                setupReportProfile_[i] = profile_[i];
                setupReportEncoder_[i] = encoder_[i];
            }
        }
        setupReportMask_ |= mask;
    }

    bool setupJogHasFeedback(long mask) const {
        for (size_t i = 0; i < profile_.size(); ++i) {
            if (!(mask & (1L << i))) continue;
            const double planned = profile_[i] - setupReportProfile_[i];
            const double measured = encoder_[i] - setupReportEncoder_[i];
            if (std::abs(planned) <= EncoderTolerancePulses ||
                std::abs(measured) <= EncoderTolerancePulses || planned * measured <= 0) return false;
        }
        return true;
    }

    void reportSetupTravel(const std::array<long, 5>& status) {
        for (size_t i = 0; i < status.size(); ++i) {
            if (!(setupReportMask_ & (1L << i))) continue;
            const double profileTravel = profile_[i] - setupReportProfile_[i];
            const double encoderTravel = encoder_[i] - setupReportEncoder_[i];
            std::printf("Web UI jog release: axis %zu profile delta %+.0f, "
                        "encoder delta %+.0f pulses, status 0x%lX.\n",
                        i + 1, profileTravel,
                        encoderTravel, static_cast<unsigned long>(status[i]));
            if (verifyReportFeedback_ && std::abs(profileTravel - encoderTravel) > EncoderTolerancePulses) {
                if (!setupFeedbackMismatch_)
                    std::fprintf(stderr, "Flex setup stopped: encoder travel did not match planned travel.\n");
                setupFeedbackMismatch_ = true;
            }
        }
        std::fflush(stdout);
        setupReportMask_ = 0;
        setupReportAt_ = 0;
    }

    void printFaultSnapshot(const std::array<long, 5>& status) const {
        std::fputs("GTS axis status:", stderr);
        for (size_t i = 0; i < status.size(); ++i)
            std::fprintf(stderr, " %zu=0x%lX", i + 1, static_cast<unsigned long>(status[i]));
        std::fputc('\n', stderr);

        // GT_GetDi reports input levels after the configured polarity transform.
        const std::array<short, 3> inputs{MC_ALARM, MC_LIMIT_POSITIVE, MC_LIMIT_NEGATIVE};
        const std::array<const char*, 3> names{"ALM", "+LIMIT", "-LIMIT"};
        std::fputs("GTS digital input levels:", stderr);
        for (size_t i = 0; i < inputs.size(); ++i) {
            long levels = 0;
            const short code = GT_GetDi(inputs[i], &levels);
            if (code) std::fprintf(stderr, " %s unavailable (%d)", names[i], code);
            else std::fprintf(stderr, " %s=0x%lX", names[i], static_cast<unsigned long>(levels));
        }
        std::fputc('\n', stderr);
    }

    bool check(short result, const char* api, short axis = 0) {
        if (!result) return true;
        if (!fault_) {
            fault_ = result;
            if (axis) std::fprintf(stderr, "%s(axis %d) failed: GTS error %d\n", api, axis, result);
            else std::fprintf(stderr, "%s failed: GTS error %d\n", api, result);
        }
        return false;
    }

    bool simulated_, opened_ = false, moving_ = false, sampled_ = false, startupFault_ = false;
    long enabled_ = 0;
    long stepMask_ = 0;
    long setupJogMask_ = 0;
    bool jogActive_ = false;
    bool trapMode_ = false;
    double speedLimit_ = DefaultSpeedLimit;
    double simulatedStepMs_ = 0.0;
    uint64_t simulatedStepAt_ = 0;
    uint64_t simulatedJogAt_ = 0;
    Axes simulatedJogVelocity_{};
    bool verifyEncoder_ = false, setupFeedbackMismatch_ = false;
    unsigned stepCorrections_ = 0;
    double stepSpeedScale_ = 1.0;
    uint64_t stepDeadline_ = 0;
    uint64_t enableReadyAt_ = 0;
    uint64_t setupReportAt_ = 0;
    long setupReportMask_ = 0;
    int fault_ = 0;
    uint32_t faultAxisStatus_ = 0;
    Axes encoder_{}, profile_{};
    Axes stepTarget_{}, stepProfile_{}, stepEncoder_{};
    Axes setupReportProfile_{}, setupReportEncoder_{};
    bool verifyReportFeedback_ = true;
};

Pose readPose(const Axes& value, const Axes& ref) {
    return {
        ((value[2] - ref[2]) - (value[0] - ref[0])) / (2 * BendPulsesPerDegree),
        ((value[3] - ref[3]) - (value[1] - ref[1])) / (2 * BendPulsesPerDegree),
        (value[4] - ref[4]) / RollPulsesPerDegree,
    };
}

bool finiteAxes(const Command& cmd) {
    return std::isfinite(cmd.x) && std::isfinite(cmd.y) && std::isfinite(cmd.roll);
}

bool within(Pose p, double bend, double roll) {
    // A coordinated step can round each axis to the nearest whole pulse.
    return std::hypot(p.x, p.y) <= bend + 1.0 / BendPulsesPerDegree &&
           std::abs(p.roll) <= roll + 0.5 / RollPulsesPerDegree;
}

bool validBendLimit(double degrees) {
    return degrees >= MinBendLimit && degrees <= MaxBendLimit && std::trunc(degrees) == degrees;
}

Pose rotateBend(Pose p, double angle) {
    const double rad = angle * Pi / 180.0;
    return {p.x * std::cos(rad) - p.y * std::sin(rad),
            p.x * std::sin(rad) + p.y * std::cos(rad), p.roll};
}

Pose nextJog(Pose current, double x, double y, double rollTarget, double bendStep) {
    const double scale = std::max(1.0, std::hypot(x, y));
    current.x += x * bendStep / scale;
    current.y += y * bendStep / scale;
    // Counter-rotate the bend in tendon coordinates to keep its plane fixed
    // in the shaft frame while the distal frame rolls about its viewing axis.
    Pose next = rotateBend(current, current.roll - rollTarget);
    next.roll = rollTarget;
    return next;
}

Pose nextPosition(Pose current, Pose target) {
    const double scale = std::max({1.0, std::hypot(target.x - current.x, target.y - current.y) / PositionBendStep,
                                  std::abs(target.roll - current.roll) / PositionRollStep});
    return {current.x + (target.x - current.x) / scale,
            current.y + (target.y - current.y) / scale,
            current.roll + (target.roll - current.roll) / scale};
}

class Driver {
public:
    Driver(bool simulated, bool manualBench, bool plannedFull, bool managedSession)
        : motion_(simulated), sprayHardware_(simulated), spray_(sprayHardware_), simulated_(simulated),
          manualBench_(manualBench), plannedFull_(plannedFull), managedSession_(managedSession) {
        status_.header = header<Status>();
        LARGE_INTEGER counter{};
        QueryPerformanceCounter(&counter);
        status_.driver = (static_cast<uint64_t>(counter.QuadPart) << 16) ^ GetCurrentProcessId();
    }

    bool open() {
        return commands_.open(L"Command") && statusMapping_.open(L"Status") &&
               vision_.open(L"Vision");
    }

    int run() {
        if (manualBench_)
            std::puts("Manual Flex control. Web UI jog and set zero. Positions are planned. Q exits.");
        if (plannedFull_)
            std::puts("Full app bench mode. Flex positions are planned, with no encoder verification. Q exits.");
        motion_.open();
        sprayHardware_.open(!motion_.fault(), gasPort, waterVelocity);
        startedAt_ = GetTickCount64();
        setupReference_ = motion_.profile();
        setupEncoder_ = motion_.encoder();
        if (!motion_.fault() && !usesPlannedFeedback())
            std::puts("Flex driver started. Use the Web UI to jog and calibrate. Q exits.");
        while (!InterlockedCompareExchange(&quitting, 0, 0)) {
            if (_kbhit() && (_getch() | 32) == 'q') break;
            const uint64_t now = GetTickCount64();
            if (!motion_.sample(now)) {
                if (!status_.fault) halt();
                status_.fault = motion_.fault();
            }
            process();
            spray_.tick(now);
            if (isSpray(status_.action) && !spray_.busy()) {
                status_.action = Idle;
                status_.result = spray_.result;
            }
            publish(now);
            Sleep(PollIntervalMs);
        }
        halt();
        const uint64_t stopDeadline = GetTickCount64() + WaterStopTimeoutMs;
        while (spray_.busy() && GetTickCount64() < stopDeadline) {
            spray_.tick(GetTickCount64());
            Sleep(PollIntervalMs);
        }
        if (spray_.busy()) std::fputs("Spray shutdown could not confirm both outputs off.\n", stderr);
        status_.stamp = 0;
        statusMapping_.write(status_);
        return motion_.fault() ? 1 : 0;
    }

    std::wstring gasPort = L"COM4";
    double waterVelocity = 30;

private:
    static bool isSpray(uint32_t action) { return action >= WaterSpray && action <= CleanLens; }
    bool usesPlannedFeedback() const { return manualBench_ || plannedFull_; }
    bool positioningReady() const { return calibrated_ || (plannedFull_ && plannedZero_); }
    const Axes& positionFeedback() const { return calibrated_ ? motion_.encoder() : motion_.profile(); }

    void halt() {
        spray_.cancel(GetTickCount64());
        motion_.stop();
        setupIntent_ = {};
        manualIntent_ = {};
        if (status_.action != Idle || status_.result == Accepted) status_.result = Canceled;
        status_.action = Idle;
    }

    void publish(uint64_t now) {
        status_.stamp = now;
        status_.fault = motion_.fault();
        status_.faultAxisStatus = motion_.faultAxisStatus();
        status_.sprayPhase = spray_.phase;
        status_.sprayError = spray_.error;
        status_.flags = RollJogSupported | CoupledRollSupported | BendLimitSupported | CombinedLimitsSupported |
                        TrackHorizonSupported | WideBendRange | TimedSpraySupported | DirectionalLookSupported |
                        (sprayHardware_.waterReady() && !spray_.error ? WaterSprayReady : 0) |
                        (sprayHardware_.gasReady() && !spray_.error ? GasSprayReady : 0) |
                        (calibrated_ ? Calibrated : 0) |
                        (motion_.moving() ? Moving : 0) | (simulated_ ? Simulated : 0) |
                        (manualBench_ ? ManualBench : 0) |
                        (plannedZero_ ? PlannedZero : 0) | (plannedFull_ ? PlannedFull : 0) |
                        (!status_.fault && atSavedZero() ? AtSavedZero : 0) |
                        (motion_.canRecheckStartup() ? StartupRecheck : 0) |
                        (!usesPlannedFeedback() && !calibrated_ && !motion_.moving() && !setupFeedbackMatches() ?
                         SetupEncoderMismatch : 0);
        const Axes& ref = calibrated_ || plannedZero_ ? reference_ : setupReference_;
        const Axes& values = positionFeedback();
        const Pose current = readPose(values, ref);
        status_.x = current.x;
        status_.y = current.y;
        status_.roll = current.roll;
        status_.bendLimit = calibrated_ || plannedZero_ ? bendLimit_ : 0.0;
        status_.speedLimit = motion_.speedLimit();
        for (size_t i = 0; i < 4; ++i) status_.tendon[i] = (values[i] - ref[i]) / 68000.0;
        if (!statusMapping_.write(status_)) halt();
    }

    void process() {
        Command cmd{};
        bool busy = false;
        const bool read = commands_.read(cmd, &busy);
        const uint64_t now = GetTickCount64();
        if (busy && fresh(lastCommandAt_, now, CommandTimeoutMs)) return;
        const bool validCommand = read && valid(cmd) && cmd.driver == status_.driver &&
                                  cmd.app && cmd.sequence && cmd.action <= DirectionalLook &&
                                  !cmd.reserved && fresh(cmd.stamp, now, CommandTimeoutMs);
        const bool heartbeatLost = !validCommand || !fresh(lastCommandAt_, now, CommandTimeoutMs);
        const bool wasConnected = status_.app != 0;
        if (wasConnected && (heartbeatLost || cmd.app != status_.app)) {
            halt();
            status_.app = 0;
        }
        if (!validCommand && managedSession_ &&
            (lastCommandAt_ ? !applicationRunning() : now - startedAt_ > 20000)) {
            std::fputs(lastCommandAt_ ? "Flex managed session ended after the Web UI closed.\n" :
                       "Flex control session timed out waiting for the Web UI.\n", stderr);
            InterlockedExchange(&quitting, 1);
            return;
        }
        if (wasConnected && heartbeatLost)
            std::fputs("Flex command heartbeat lost. Motion stopped. Waiting for the Web UI.\n", stderr);
        if (!validCommand) return;
        lastCommandAt_ = cmd.stamp;
        if (cmd.action == Stop && cmd.sequence >= status_.sequence) {
            halt();
            acknowledge(cmd, spray_.busy() ? Accepted : (motion_.fault() ? Refused : Done));
            blockedSequence_ = cmd.sequence;
            waitForNeutral_ = false;
            return;
        }
        if (motion_.fault()) {
            if (cmd.action == RecheckStartup && cmd.request &&
                (cmd.app != recheckApp_ || cmd.sequence > status_.sequence) &&
                motion_.canRecheckStartup()) {
                recheckApp_ = cmd.app;
                if (motion_.recheckStartup()) {
                    setupReference_ = motion_.profile();
                    setupEncoder_ = motion_.encoder();
                    acknowledge(cmd, Done);
                } else {
                    acknowledge(cmd, Refused);
                }
            }
            return;
        }
        // Settle the drives when the app connects with neutral intent, before acknowledging
        // the session and exposing jog controls.
        if (!motion_.ready()) {
            if (cmd.action == Idle) motion_.enable();
            return;
        }
        if (!status_.app) {
            if (cmd.action != Idle) return;
            status_.app = cmd.app;
            status_.sequence = cmd.sequence;
            status_.request = 0;
            status_.result = None;
            blockedSequence_ = cmd.sequence;
            waitForNeutral_ = false;
        }

        if (spray_.busy()) {
            if (cmd.action != status_.action || cmd.request != status_.request || cmd.sequence != status_.sequence)
                spray_.cancel(now);
            return;
        }
        if (waitForNeutral_) {
            if (cmd.action != Idle) return;
            waitForNeutral_ = false;
        }
        if (cmd.sequence > blockedSequence_ && cmd.sequence >= status_.sequence) {
            if (isSpray(cmd.action)) {
                if (!cmd.request || cmd.request <= status_.request) return;
                if (motion_.moving()) return;
                if ((status_.action != Idle && status_.action != Horizon) ||
                    !finiteAxes(cmd) || (cmd.x != 0. && (cmd.x != 3. || cmd.action == CleanLens)) || cmd.y || cmd.roll) {
                    acknowledge(cmd, Refused);
                    return;
                }
                motion_.stop();
                const bool started = spray_.start(static_cast<Action>(cmd.action), now, cmd.x == 3.);
                acknowledge(cmd, started ? Accepted : Refused);
                if (started) status_.action = cmd.action;
                return;
            }
            if ((status_.action == Position || status_.action == ReturnZero) &&
                (cmd.action != status_.action || cmd.sequence != status_.sequence || cmd.request != status_.request)) {
                halt();
                blockedSequence_ = cmd.sequence;
                return;
            }
            if (cmd.request && cmd.request > status_.request && status_.action == Horizon) halt();
            if (cmd.request && cmd.request > status_.request && status_.action == Idle &&
                motion_.moving()) return;
            if (cmd.request && cmd.request > status_.request) acceptRequest(cmd);
            if (cmd.action == Idle || cmd.action == Jog || cmd.action == RollJog ||
                cmd.action == Track || cmd.action == DirectionalLook || cmd.action == Horizon) {
                if (!calibrated_ && !plannedZero_) serviceSetupJog(cmd);
                else serviceContinuous(cmd);
            }
            if (cmd.action == Position && cmd.request == status_.request && status_.result == Accepted)
                servicePosition(cmd);
            if (cmd.action == ReturnZero && cmd.request == status_.request && status_.result == Accepted)
                serviceReturnZero(cmd);
        }
    }

    void acknowledge(const Command& cmd, Result result) {
        status_.sequence = cmd.sequence;
        status_.request = cmd.request;
        status_.result = result;
    }

    void acceptRequest(const Command& cmd) {
        if (cmd.action == SetSpeedLimit) {
            if (status_.action != Idle || motion_.moving() || !std::isfinite(cmd.x) || cmd.x < MinSpeedLimit) {
                acknowledge(cmd, Refused);
                return;
            }
            motion_.setSpeedLimit(cmd.x);
            acknowledge(cmd, Done);
            return;
        }
        if (cmd.action == SetBendLimit) {
            if ((!calibrated_ && !plannedZero_) || status_.action != Idle || motion_.moving() ||
                !validBendLimit(cmd.x) ||
                (cmd.y != 0.0 && (!std::isfinite(cmd.y) || cmd.y < MinSpeedLimit))) {
                acknowledge(cmd, Refused);
                return;
            }
            const Pose pose = readPose(positionFeedback(), reference_);
            if (std::hypot(pose.x, pose.y) > cmd.x + 1e-6) {
                acknowledge(cmd, Limit);
                return;
            }
            bendLimit_ = cmd.x;
            if (cmd.y != 0.0) motion_.setSpeedLimit(cmd.y);
            acknowledge(cmd, Done);
            return;
        }
        if (cmd.action == ReturnZero) {
            if ((!calibrated_ && !plannedZero_) || status_.action != Idle || motion_.moving()) {
                acknowledge(cmd, Refused);
                return;
            }
            Pose travel = readPose(reference_, travelReference_);
            if (plannedZero_) travel.roll = readPose(reference_, setupReference_).roll;
            if (!within(travel, MaxBendLimit, RollLimit)) {
                acknowledge(cmd, Limit);
                return;
            }
            positionStarted_ = false;
            status_.action = ReturnZero;
            acknowledge(cmd, Accepted);
            return;
        }
        if (cmd.action == Calibrate) {
            if (status_.action != Idle || motion_.moving()) {
                acknowledge(cmd, Refused);
                return;
            }
            if (cmd.x != 0.0 && !validBendLimit(cmd.x)) {
                acknowledge(cmd, Refused);
                return;
            }
            const bool planned = usesPlannedFeedback();
            if (!planned && !calibrated_ && !setupFeedbackMatches()) {
                std::fprintf(stderr, "Calibration refused: setup profile and encoder travel disagree. "
                                     "Check the GTS encoder wiring, direction, and pulse scale.\n");
                acknowledge(cmd, Refused);
                return;
            }
            reference_ = planned ? motion_.profile() : motion_.encoder();
            if (!calibrated_ && !plannedZero_) travelReference_ = reference_;
            plannedZero_ = planned;
            calibrated_ = !planned;
            if (cmd.x != 0.0) bendLimit_ = cmd.x;
            acknowledge(cmd, Done);
            return;
        }
        if (cmd.action == Position) {
            if (!positioningReady() || motion_.moving() || status_.action != Idle || !finiteAxes(cmd)) {
                acknowledge(cmd, Refused);
                return;
            }
            positionTarget_ = {cmd.x, cmd.y, cmd.roll};
            if (!withinTravel(positionTarget_, reference_)) {
                acknowledge(cmd, Limit);
                return;
            }
            positionStarted_ = false;
            status_.action = Position;
            acknowledge(cmd, Accepted);
        }
    }

    void servicePosition(const Command& cmd) {
        if (motion_.moving()) return;
        const Pose current = readPose(positionFeedback(), reference_);
        const Pose target = positionTarget_;
        if (std::hypot(target.x - current.x, target.y - current.y) <= PositionTolerance &&
            std::abs(target.roll - current.roll) <= PositionTolerance) {
            status_.action = Idle;
            acknowledge(cmd, Done);
            return;
        }
        if (!plannedFull_) {
            move(nextPosition(current, target), reference_);
        } else if (positionStarted_) {
            halt();
            acknowledge(cmd, Refused);
        } else if (!motion_.position(target, reference_, true)) {
            halt();
        } else {
            positionStarted_ = motion_.moving();
        }
    }

    bool atSavedZero() const {
        if ((!calibrated_ && !plannedZero_) || motion_.moving()) return false;
        const Axes& values = plannedZero_ ? motion_.profile() : motion_.encoder();
        const double tolerance = plannedZero_ ? ProfileTolerancePulses : EncoderTolerancePulses;
        for (size_t i = 0; i < values.size(); ++i) {
            const double target = plannedZero_ ? std::round(reference_[i]) : reference_[i];
            if (std::abs(values[i] - target) > tolerance) return false;
        }
        return true;
    }

    void serviceReturnZero(const Command& cmd) {
        if (motion_.moving()) return;
        if (atSavedZero()) {
            status_.action = Idle;
            acknowledge(cmd, Done);
            return;
        }
        if (positionStarted_) {
            halt();
            acknowledge(cmd, Refused);
            return;
        }
        if (!motion_.returnToZero(reference_, plannedZero_)) {
            halt();
            return;
        }
        positionStarted_ = motion_.moving();
    }

    void serviceContinuous(const Command& cmd) {
        if (cmd.action == Idle) {
            if (status_.action != Idle) halt();
            status_.sequence = cmd.sequence;
            return;
        }
        const bool manualRoll = cmd.action == RollJog;
        const bool manual = cmd.action == Jog || manualRoll;
        if ((!positioningReady() && !manual) || !finiteAxes(cmd) ||
            ((cmd.action == Horizon || manualRoll) && (cmd.x || cmd.y)) ||
            std::max({std::abs(cmd.x), std::abs(cmd.y), std::abs(cmd.roll)}) > 1.0) {
            status_.sequence = cmd.sequence;
            halt();
            status_.result = Refused;
            blockedSequence_ = cmd.sequence;
            return;
        }
        if (cmd.action == Track) {
            Vision msg{};
            if (!vision_.read(msg) || !valid(msg) || msg.driver != status_.driver ||
                msg.app != cmd.app || msg.frame < cmd.frame || !msg.valid || msg.centered ||
                msg.category < 0 || !msg.width || !msg.height ||
                !std::isfinite(msg.dx) || !std::isfinite(msg.dy) ||
                !fresh(msg.captured, GetTickCount64(), VisionTimeoutMs)) {
                halt();
                blockedSequence_ = cmd.sequence;
                return;
            }
            if (status_.action == Track && msg.revision != trackingRevision_) {
                halt();
                blockedSequence_ = cmd.sequence;
                waitForNeutral_ = true;
                return;
            }
            trackingRevision_ = msg.revision;
        }
        status_.sequence = cmd.sequence;
        if (!cmd.x && !cmd.y && !cmd.roll) {
            if (status_.action != Idle) halt();
            return;
        }
        const std::array<double, 3> intent = manual
            ? std::array<double, 3>{cmd.x, cmd.y, manualRoll ? cmd.roll : 0.0}
            : std::array<double, 3>{};
        if (intent != manualIntent_) {
            if ((status_.action != Idle || motion_.moving()) && !motion_.stop()) return;
            manualIntent_ = intent;
        }
        status_.action = manualRoll ? Jog : cmd.action;
        status_.result = Accepted;
        if (motion_.moving()) return;
        const Pose current = readPose(positionFeedback(), reference_);
        const bool horizon = cmd.action == Horizon;
        const bool tracking = cmd.action == Track || cmd.action == DirectionalLook;
        const double speed = motion_.speedLimit();
        const double bendStep = horizon || manualRoll ? 0.0 : tracking ? TrackBendStep :
                                std::min(speed, JogBendRate) * JogSegmentSeconds;
        double rollStep = horizon ? HorizonRollStep : tracking ? TrackRollStep :
                          std::min(speed, JogRollRate) * JogSegmentSeconds;
        if (manualRoll) {
            const double bend = std::hypot(current.x, current.y);
            if (bend <= StraightRollBendTolerance && std::abs(cmd.roll) == 1.0) {
                // A straight scope needs no material tendon compensation.
                // Target the travel limit so roll can stay at speed while held.
                rollStep = 2 * RollLimit;
            } else {
                const double radius = bend * Pi / 180.0;
                const double rate = std::min({JogRollRate, speed / std::hypot(1.0, radius),
                                              JogBendRate / std::max(radius, 1e-9)});
                const double chordStep = std::sqrt(8.0 * RollChordTolerance / bend) * 180.0 / Pi;
                rollStep = std::min(rate * JogSegmentSeconds, chordStep);
            }
        }
        const double zeroRoll = readPose(reference_, usesPlannedFeedback() ? setupReference_ : travelReference_).roll;
        const double requestedRoll = cmd.roll * rollStep;
        const double rollDelta = std::clamp(requestedRoll,
            std::min(0.0, std::max(-RollLimit, -RollLimit - zeroRoll) - current.roll),
            std::max(0.0, std::min(RollLimit, RollLimit - zeroRoll) - current.roll));
        const Pose next = manual ? boundedJog(current, cmd.x, cmd.y, rollDelta, bendStep) :
                          nextJog(current, cmd.x, cmd.y, current.roll + rollDelta, bendStep);
        const bool noPulse = std::max({std::abs(next.x - current.x) * BendPulsesPerDegree,
                                       std::abs(next.y - current.y) * BendPulsesPerDegree,
                                       std::abs(next.roll - current.roll) * RollPulsesPerDegree}) < 1.0;
        // Tracking requires the commanded roll and tendon motion together.
        if (noPulse || (tracking && std::abs(rollDelta - requestedRoll) > 1e-6) ||
            !withinTravel(next, reference_)) {
            halt();
            status_.result = Limit;
            blockedSequence_ = cmd.sequence;
            return;
        }
        const double speedScale = horizon ? HorizonSpeedScale : tracking ? TrackSpeedScale : JogSpeedScale;
        move(next, reference_, speedScale);
    }

    Pose boundedJog(Pose current, double x, double y, double rollDelta, double bendStep) const {
        const auto poseAt = [&](double fraction) {
            return nextJog(current, x, y, current.roll + fraction * rollDelta, fraction * bendStep);
        };
        const Pose target = poseAt(1.0);
        if (withinTravel(target, reference_)) return target;
        double lower = 0.0, upper = 1.0;
        for (int i = 0; i < 20; ++i) {
            const double mid = (lower + upper) / 2.0;
            if (withinTravel(poseAt(mid), reference_)) lower = mid;
            else upper = mid;
        }
        return poseAt(lower);
    }

    void serviceSetupJog(const Command& cmd) {
        status_.sequence = cmd.sequence;
        if (cmd.action == Idle) {
            if (status_.action != Idle) halt();
            return;
        }
        const auto direction = [](double v) { return v == -1 || v == 0 || v == 1; };
        const bool bendJog = cmd.action == Jog && direction(cmd.x) && direction(cmd.y) && cmd.roll == 0;
        const bool rollJog = cmd.action == RollJog && direction(cmd.roll) && cmd.x == 0 && cmd.y == 0;
        if (!bendJog && !rollJog) {
            halt();
            status_.result = Refused;
            blockedSequence_ = cmd.sequence;
            return;
        }
        const std::array<int, 3> intent{int(cmd.x), int(cmd.y), int(cmd.roll)};
        if (intent == std::array<int, 3>{}) {
            if (status_.action != Idle) halt();
            return;
        }
        if (intent != setupIntent_) {
            if (setupIntent_ != std::array<int, 3>{} && !motion_.stop()) return;
            setupIntent_ = intent;
        }
        const auto [x, y, roll] = intent;
        const Axes& profile = motion_.profile();
        const bool verifyFeedback = !usesPlannedFeedback();
        if (verifyFeedback && !motion_.moving() && !setupFeedbackMatches()) {
            halt();
            status_.result = Refused;
            blockedSequence_ = cmd.sequence;
            return;
        }
        status_.action = Jog;
        status_.result = Accepted;
        if (!roll) {
            if (!motion_.setupJog(x, y, verifyFeedback)) halt();
            return;
        }
        if (motion_.moving()) return;
        const double current = profile[4];
        const double travel = std::floor(RollLimit * RollPulsesPerDegree);
        const double lower = std::min(current, std::ceil(setupReference_[4] - travel));
        const double upper = std::max(current, std::floor(setupReference_[4] + travel));
        const double target = roll > 0 ? upper : lower;
        if (std::abs(target - current) < 1.0) {
            halt();
            status_.result = Limit;
            blockedSequence_ = cmd.sequence;
        } else if (!motion_.rollBy(target - current, verifyFeedback)) {
            halt();
        }
    }

    bool setupFeedbackMatches() const {
        if (motion_.setupFeedbackMismatch()) return false;
        for (size_t i = 0; i < setupEncoder_.size(); ++i) {
            const double measured = motion_.encoder()[i] - setupEncoder_[i];
            const double planned = motion_.profile()[i] - setupReference_[i];
            if (std::abs(measured - planned) > EncoderTolerancePulses) return false;
        }
        return true;
    }

    bool withinTravel(Pose next, const Axes& reference) const {
        if (!within(next, bendLimit_, RollLimit)) return false;
        if (!calibrated_ && !plannedZero_) return true;
        const Pose zero = readPose(reference, travelReference_);
        const double zeroRoll = usesPlannedFeedback() ? readPose(reference, setupReference_).roll : zero.roll;
        return within({next.x + zero.x, next.y + zero.y, next.roll + zeroRoll}, MaxBendLimit, RollLimit);
    }

    void move(Pose next, const Axes& reference, double speedScale = 1.0) {
        if (!withinTravel(next, reference)) {
            motion_.stop();
            status_.result = Limit;
            status_.action = Idle;
            blockedSequence_ = status_.sequence;
        } else if (!motion_.position(next, reference, usesPlannedFeedback(), speedScale)) {
            halt();
        }
    }

    Motion motion_;
    SprayHardware sprayHardware_;
    SprayControl spray_;
    bool simulated_, calibrated_ = false;
    bool manualBench_;
    bool plannedFull_;
    bool plannedZero_ = false;
    double bendLimit_ = DefaultBendLimit;
    bool positionStarted_ = false;
    bool managedSession_ = false;
    uint64_t startedAt_ = 0;
    uint64_t lastCommandAt_ = 0;
    std::array<int, 3> setupIntent_{};
    std::array<double, 3> manualIntent_{};
    bool waitForNeutral_ = false;
    uint64_t blockedSequence_ = 0;
    uint64_t recheckApp_ = 0;
    uint32_t trackingRevision_ = 0;
    Axes reference_{}, setupReference_{}, setupEncoder_{}, travelReference_{};
    Pose positionTarget_{};
    Status status_{};
    Mapping<Command> commands_;
    Mapping<Status> statusMapping_;
    Mapping<Vision> vision_;
};
}

int wmain(int argc, wchar_t** argv) {
    bool simulated = false, manualBench = false, plannedFull = false, managedSession = false;
    std::wstring gasPort = L"COM4";
    double waterVelocity = 30;
    for (int i = 1; i < argc; ++i) {
        if (std::wcscmp(argv[i], L"--simulate") == 0) simulated = true;
        else if (std::wcscmp(argv[i], L"--manual-bench") == 0) manualBench = true;
        else if (std::wcscmp(argv[i], L"--planned-full") == 0) plannedFull = true;
        else if (std::wcscmp(argv[i], L"--managed-session") == 0) managedSession = true;
        else if (std::wcscmp(argv[i], L"--gas-port") == 0 && i + 1 < argc) {
            gasPort = argv[++i];
            if (gasPort.size() < 4 || gasPort.substr(0, 3) != L"COM" ||
                gasPort.find_first_not_of(L"0123456789", 3) != std::wstring::npos) return 2;
        }
        else if (std::wcscmp(argv[i], L"--water-velocity") == 0 && i + 1 < argc) {
            wchar_t* end = nullptr;
            waterVelocity = std::wcstod(argv[++i], &end);
            if (!end || *end || !std::isfinite(waterVelocity) || waterVelocity == 0 ||
                std::abs(waterVelocity) > 30) return 2;
        }
        else {
            std::fprintf(stderr, "Unknown Flex driver option.\n");
            return 2;
        }
    }
    if (int(manualBench) + int(plannedFull) > 1) {
        std::fprintf(stderr, "Conflicting Flex driver options.\n");
        return 2;
    }
    const HANDLE singleton = CreateMutexW(nullptr, FALSE, DriverMutex);
    if (!singleton || GetLastError() == ERROR_ALREADY_EXISTS) {
        if (singleton) CloseHandle(singleton);
        std::fprintf(stderr, "A Flex driver is already running, or the session mutex is unavailable.\n");
        return 1;
    }
    SetConsoleCtrlHandler(consoleEvent, TRUE);
    int result = 1;
    {
        Driver driver(simulated, manualBench, plannedFull, managedSession);
        driver.gasPort = gasPort;
        driver.waterVelocity = waterVelocity;
        if (driver.open()) result = driver.run();
        else std::fprintf(stderr, "Could not initialize Flex shared memory.\n");
    }
    CloseHandle(singleton);
    return result;
}
