// Native completion regression tests. SDK reads are replaced below; this
// executable never calls Motion::open(), Driver::open(), or Driver::run().
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <conio.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cwchar>
#include <limits>
#include <string>

// Compile the real completion code in an isolated test executable. Standard
// headers are included first to keep this access override local to the driver.
#define private public
#define wmain unusedDriverEntry
#include "../src/flex_endo_robo/flex_endo/FlexEndoscopeDriver/FlexEndoscopeDriver.cpp"
#undef wmain
#undef private

namespace {
Axes fakeProfile{}, fakeEncoder{};
std::array<long, 5> fakeStatus{};
std::array<TTrapPrm, 5> fakeTrap{};
std::array<long, 5> fakeTarget{};
Axes fakeVelocity{};
long updatedMask = 0;
short trapFailure = 0;
int checks = 0;

void require(bool condition, const char* message) {
    if (!condition) {
        std::fprintf(stderr, "FAILED: %s\n", message);
        std::exit(1);
    }
    ++checks;
}

void prepare(Motion& motion, double profile, double encoder = 0., bool verify = false) {
    fakeProfile.fill(0.);
    fakeEncoder.fill(0.);
    fakeStatus.fill(0);
    fakeProfile[4] = profile;
    fakeEncoder[4] = encoder;
    motion.stepMask_ = 1L << 4;
    motion.stepTarget_[4] = 629.;
    motion.verifyEncoder_ = verify;
    motion.stepDeadline_ = 100;
}
}

GT_API GT_GetPrfPos(short first, double* values, short count, unsigned long*) {
    std::copy_n(fakeProfile.begin() + first - 1, count, values);
    return 0;
}
GT_API GT_GetEncPos(short first, double* values, short count, unsigned long*) {
    std::copy_n(fakeEncoder.begin() + first - 1, count, values);
    return 0;
}
GT_API GT_GetSts(short first, long* values, short count, unsigned long*) {
    std::copy_n(fakeStatus.begin() + first - 1, count, values);
    return 0;
}
GT_API GT_GetDi(short, long* values) {
    *values = 0;
    return 0;
}
GT_API GT_PrfTrap(short) { return 0; }
GT_API GT_SetTrapPrm(short axis, TTrapPrm* parameters) {
    fakeTrap[axis - 1] = *parameters;
    if (trapFailure) return trapFailure;
    return (std::isfinite(parameters->acc) && parameters->acc >= MinTrapAcceleration &&
            std::isfinite(parameters->dec) && parameters->dec >= MinTrapAcceleration) ? 0 : 7;
}
GT_API GT_SetPos(short axis, long target) {
    fakeTarget[axis - 1] = target;
    return 0;
}
GT_API GT_SetVel(short axis, double velocity) {
    fakeVelocity[axis - 1] = velocity;
    return std::isfinite(velocity) && velocity > 0. ? 0 : 7;
}
GT_API GT_Update(long mask) {
    updatedMask = mask;
    return 0;
}

int main() {
    {
        Motion motion(false);
        prepare(motion, 634.);  // Reported Bird-eye failure: target 629, encoder 0.
        require(motion.sample(100), "five-pulse settled planned residual must complete");
        require(!motion.moving() && !motion.fault(), "settled residual must clear the pending step");
    }
    for (double error : {-8., 8.}) {
        Motion motion(false);
        prepare(motion, 629. + error);
        require(motion.sample(100), "both tolerance boundaries must complete");
    }
    for (double error : {-64.01, 64.01, 100.}) {
        Motion motion(false);
        prepare(motion, 629. + error);
        require(!motion.sample(100) && motion.fault() == -112,
                "residuals above the correction bound must retain the profile fault");
    }
    {
        Motion motion(false);
        prepare(motion, 992.);
        motion.stepTarget_[4] = 1014.;
        require(motion.sample(100), "reported 22-pulse stopped residual must get a bounded correction");
        require(updatedMask == (1L << 4) && fakeTarget[4] == 1014,
                "correction must retain the original roll endpoint and leave other axes alone");
        require(motion.stepCorrections_ == 1 && motion.moving(), "a correction must wait for fresh completion");
        fakeProfile[4] = 1014.;
        require(motion.sample(101) && !motion.moving(), "corrected profile must complete from feedback");
    }
    {
        Motion motion(false);
        prepare(motion, 992.);
        motion.stepTarget_[4] = 1014.;
        for (unsigned i = 0; i < MaxProfileCorrections; ++i)
            require(motion.sample(motion.stepDeadline_), "small planned residual may be retried within its budget");
        require(!motion.sample(motion.stepDeadline_) && motion.fault() == -112,
                "a stopped axis that does not progress must fault after two corrections");
    }
    {
        Motion motion(false);
        prepare(motion, 992., 1014., true);
        motion.stepTarget_[4] = 1014.;
        require(!motion.sample(100) && motion.fault() == -112 && motion.stepCorrections_ == 0,
                "planned residual recovery must not bypass encoder-mode profile checks");
    }
    {
        Motion motion(false);
        prepare(motion, 634.);
        fakeStatus[4] = ProfileMoving;
        require(motion.sample(99) && motion.moving(), "a moving profile must not complete early");
        require(!motion.sample(100) && motion.fault() == -112,
                "a moving profile near its target must still time out");
    }
    {
        Motion motion(false);
        prepare(motion, 634., 0., true);
        require(!motion.sample(100) && motion.fault() == -113,
                "profile tolerance must not hide missing encoder travel");
    }
    {
        Motion motion(false);
        prepare(motion, 634., 629., true);
        require(motion.sample(100), "settled profile with matching encoder travel must complete");
    }
    {
        Motion motion(false);
        prepare(motion, 634., 655., true);
        require(!motion.sample(100) && motion.fault() == -113,
                "encoder errors above 25 pulses must still fail");
    }
    {
        Driver driver(false, false, true, false);
        driver.plannedZero_ = true;
        driver.reference_[4] = 629.;
        driver.motion_.profile_[4] = 634.;
        require(driver.atSavedZero(), "saved-zero completion must use the same planned tolerance");
        driver.motion_.profile_[4] = 638.;
        require(!driver.atSavedZero(), "saved zero must reject a larger residual");
    }
    {
        Motion motion(false);
        const Axes target{0., -30. * BendPulsesPerDegree, 0., 30. * BendPulsesPerDegree, 1.};
        require(motion.positionAxes(target, false, false),
                "Bird-eye with large tendon travel and one roll pulse must use valid trap parameters");
        require(fakeTrap[4].acc == MinTrapAcceleration,
                "tiny roll acceleration must not collapse in proportion to tendon travel");
        double finish = 0.;
        for (size_t i : {size_t(1), size_t(3), size_t(4)}) {
            const double distance = std::abs(fakeTarget[i]);
            const double velocity = fakeVelocity[i];
            const double totalMs = distance / velocity + velocity / fakeTrap[i].acc + 2 * TrapSmoothTimeMs;
            require(velocity <= (i == 4 ? RollPositionSpeed : BendPositionSpeed) + 1e-9,
                    "synchronized trapezoids must retain pulse speed caps");
            require(std::isfinite(totalMs) && (finish == 0. || std::abs(totalMs - finish) < 1e-6),
                    "floored accelerations must still share a common completion time");
            finish = totalMs;
        }
    }
    {
        Motion motion(false);
        trapFailure = 7;
        require(!motion.positionAxes(Axes{0., 0., 0., 0., 10.}, false, false) && motion.fault() == 7,
                "a real SDK rejection must still fault instead of being ignored");
        trapFailure = 0;
    }
    std::printf("Passed %d native completion checks. No hardware was opened.\n", checks);
    return 0;
}
