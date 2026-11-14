#pragma once

#include <cstddef>
#include <cstdint>

namespace flex {
constexpr uint32_t Magic = 0x58454c46;  // FLEX
constexpr uint32_t Version = 1;
constexpr wchar_t Prefix[] = L"Local\\CRFFlexV1";
constexpr uint64_t CommandTimeoutMs = 350;
constexpr uint64_t VisionTimeoutMs = 500;

enum Action : uint32_t { Idle, Stop, Calibrate, SetMode, Jog, Position, Track, ReturnZero };
enum Mode : uint32_t { Fixed, Free };
enum Flags : uint32_t {
    Calibrated = 1, Setup = 2, Moving = 4, Simulated = 8, Xbox = 16, SetupEncoderMismatch = 32,
    ManualBench = 64, PlannedZero = 128, PlannedFull = 256
};
enum Result : uint32_t { None, Accepted, Done, Refused, Canceled, Limit };

#pragma pack(push, 1)
struct Header {
    uint32_t magic, version, bytes, reserved;
};
struct Command {
    Header header;
    uint64_t driver, app, sequence, stamp, request, frame;
    uint32_t action, mode;
    double x, y, roll;
};
struct Status {
    Header header;
    uint64_t driver, stamp, app, sequence, request;
    uint32_t action, mode, flags, result;
    int32_t fault;
    uint32_t faultAxisStatus;
    double x, y, roll, bendLimit, tendon[4];
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
static_assert(sizeof(Status) == 144 && offsetof(Status, x) == 80);
static_assert(sizeof(Vision) == 88 && offsetof(Vision, dx) == 64);

template<class T> constexpr Header header() { return {Magic, Version, sizeof(T), 0}; }
template<class T> bool valid(const T& msg) {
    return msg.header.magic == Magic && msg.header.version == Version && msg.header.bytes == sizeof(T);
}
inline bool fresh(uint64_t stamp, uint64_t now, uint64_t ttl) {
    return stamp && stamp <= now && now - stamp <= ttl;
}
}
