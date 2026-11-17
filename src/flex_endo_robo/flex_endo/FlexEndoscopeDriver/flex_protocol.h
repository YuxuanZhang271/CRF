#pragma once

#include <cstddef>
#include <cstdint>

namespace flex {
constexpr uint32_t Magic = 0x58454c46;  // FLEX
constexpr uint32_t Version = 4;
constexpr wchar_t Prefix[] = L"Local\\CRFFlexV4";
// All driver versions must share the same hardware ownership lock.
constexpr wchar_t DriverMutex[] = L"Local\\CRFFlexV1Driver";
// Match the ownership mutex that FlexClient holds open for its lifetime.
constexpr wchar_t ApplicationMutex[] = L"Local\\CRFFlexV1Application";
constexpr uint64_t CommandTimeoutMs = 350;
constexpr uint64_t VisionTimeoutMs = 500;

enum Action : uint32_t {
    Idle, Stop, Calibrate, Horizon, Jog, Position, Track, ReturnZero,
    RecheckStartup, RollJog, SetBendLimit, SetSpeedLimit
};
enum Flags : uint32_t {
    Calibrated = 1, Moving = 4, Simulated = 8, SetupEncoderMismatch = 32,
    ManualBench = 64, PlannedZero = 128, PlannedFull = 256, StartupRecheck = 512,
    RollJogSupported = 1024, BendLimitSupported = 2048,
    TrackHorizonSupported = 4096, WideBendRange = 8192, AtSavedZero = 16384,
    CoupledRollSupported = 32768, CombinedLimitsSupported = 65536
};
enum Result : uint32_t { None, Accepted, Done, Refused, Canceled, Limit };

#pragma pack(push, 1)
struct Header {
    uint32_t magic, version, bytes, reserved;
};
struct Command {
    Header header;
    uint64_t driver, app, sequence, stamp, request, frame;
    uint32_t action, reserved;
    double x, y, roll;
};
struct Status {
    Header header;
    uint64_t driver, stamp, app, sequence, request;
    uint32_t action, reserved, flags, result;
    int32_t fault;
    uint32_t faultAxisStatus;
    double x, y, roll, bendLimit, tendon[4], speedLimit;
};
struct Vision {
    Header header;
    uint64_t driver, app, frame, captured;
    uint32_t valid, width, height, centered;
    double dx, dy;
    int32_t category;
    uint32_t revision;
};
#pragma pack(pop)

static_assert(sizeof(Command) == 96 && offsetof(Command, x) == 72);
static_assert(sizeof(Status) == 152 && offsetof(Status, x) == 80 && offsetof(Status, speedLimit) == 144);
static_assert(sizeof(Vision) == 88 && offsetof(Vision, dx) == 64);

template<class T> constexpr Header header() { return {Magic, Version, sizeof(T), 0}; }
template<class T> bool valid(const T& msg) {
    return msg.header.magic == Magic && msg.header.version == Version && msg.header.bytes == sizeof(T);
}
inline bool fresh(uint64_t stamp, uint64_t now, uint64_t ttl) {
    return stamp && stamp <= now && now - stamp <= ttl;
}
}
