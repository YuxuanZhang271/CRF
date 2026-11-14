#include <cstdio>
#include <cmath>
#include "stdafx.h"
#include <Windows.h>
#include <Xinput.h>
#include <conio.h>
#include "gts.h"

#define axisNumber1 1
#define axisNumber2 2
#define axisNumber3 3
#define axisNumber4 4
#define axisNumber5 5
#define axisNumber6 6

#define bendingVelocity 110
#define rotationVelocity 18
#define axis6Velocity 30

#define jogacc 100
#define jogdec 100
#define smoothtime 1
#define selfRotationAngle 4

/* ================= 共享内存（和 bottle.py 对齐） ================= */
#pragma pack(push, 1)
struct SharedVision
{
    volatile LONG  mode;      // 0=手柄 1=YOLO
    volatile LONG  locked;    // 1=bottle 中心已进入死区
    volatile FLOAT dx;        // 像素：bottle中心 - 画面中心
    volatile FLOAT dy;
    volatile LONG  seq;       // 帧序号
    LONG reserved[7];         // 填充到 48 字节
};
#pragma pack(pop)

static HANDLE        gShmHandle = nullptr;
static SharedVision* gVision = nullptr;

static bool InitSharedVision()
{
    gShmHandle = CreateFileMappingW(
        INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
        0, sizeof(SharedVision), L"EndoVisionShared");
    if (!gShmHandle) return false;

    gVision = (SharedVision*)MapViewOfFile(
        gShmHandle, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(SharedVision));
    if (!gVision)
    {
        CloseHandle(gShmHandle);
        gShmHandle = nullptr;
        return false;
    }
    ZeroMemory((void*)gVision, sizeof(SharedVision));
    return true;
}

static void CloseSharedVision()
{
    if (gVision) { UnmapViewOfFile(gVision); gVision = nullptr; }
    if (gShmHandle) { CloseHandle(gShmHandle);  gShmHandle = nullptr; }
}

/* ============ 弯曲角度约束 ============ */
static const double PULSE_PER_MM = 68000.0;
static const double ROD_RADIUS_MM = 4.0;
static const double MAX_BEND_RAD = 30.0 * 3.141592653589793 / 180.0;
static const double MAX_BEND_LEN_MM = ROD_RADIUS_MM * MAX_BEND_RAD;
static const double BEND_STEP_MM = 0.01;

static double GetCurrentBendLen()
{
    double p3 = 0, p4 = 0;
    GT_GetPrfPos(axisNumber3, &p3);
    GT_GetPrfPos(axisNumber4, &p4);
    double d3 = p3 / PULSE_PER_MM;
    double d4 = p4 / PULSE_PER_MM;
    return std::sqrt(d3 * d3 + d4 * d4);
}

static bool CanBendStep(int which, int axisSign)
{
    double p3 = 0, p4 = 0;
    GT_GetPrfPos(axisNumber3, &p3);
    GT_GetPrfPos(axisNumber4, &p4);
    double d3 = p3 / PULSE_PER_MM;
    double d4 = p4 / PULSE_PER_MM;

    double currLen2 = d3 * d3 + d4 * d4;

    if (which == 3) d3 += axisSign * BEND_STEP_MM;
    else            d4 += axisSign * BEND_STEP_MM;

    double nextLen2 = d3 * d3 + d4 * d4;

    // 【修复】如果运动趋势是减小当前弯曲度，即使是超限状态，也必须放行让它回退！
    if (nextLen2 < currLen2) return true;

    // 否则检查是否在限制内
    return nextLen2 <= MAX_BEND_LEN_MM * MAX_BEND_LEN_MM;
}
/* ===================================== */

/* ============ YOLO 跟踪常量 & 状态 ============ */
static const BYTE  TRIG_THRESHOLD = 120;
static const float PIXEL_DEADZONE = 25.0f;

static bool gTrackingMode = false;
static bool gLastBothTriggers = false;
static int  sLastV13 = 0;
static int  sLastV24 = 0;

void commandhandler(char* command, short error)
{
    if (error) printf("%s = %d\n", command, error);
}

int GetControllerNum()
{
    int num = 0;
    for (DWORD i = 0; i < XUSER_MAX_COUNT; i++)
    {
        XINPUT_STATE state;
        ZeroMemory(&state, sizeof(XINPUT_STATE));
        if (XInputGetState(i, &state) == ERROR_SUCCESS) num++;
    }
    return num;
}

bool IsControllerConnected()
{
    XINPUT_STATE state;
    return (XInputGetState(0, &state) == ERROR_SUCCESS);
}

void EmergencyStop()
{
    short sRtn = GT_Stop(0x3F, 0x3F);
    commandhandler((char*)"GT_Stop(All)", sRtn);
    for (int i = 1; i <= 6; i++) {
        sRtn = GT_AxisOff(i);
        printf("GT_AxisOff(%d)=%d\n", i, sRtn);
    }
}

void InitAxisJog(int axisNumber, double acc, double dec)
{
    short sRtn;
    TJogPrm jog;
    sRtn = GT_AxisOn(axisNumber);          commandhandler((char*)"GT_AxisOn", sRtn);
    sRtn = GT_ZeroPos(axisNumber);         commandhandler((char*)"GT_ZeroPos", sRtn);
    sRtn = GT_PrfJog(axisNumber);          commandhandler((char*)"GT_PrfJog", sRtn);
    sRtn = GT_GetJogPrm(axisNumber, &jog); commandhandler((char*)"GT_GetJogPrm", sRtn);
    jog.acc = acc; jog.dec = dec;
    sRtn = GT_SetJogPrm(axisNumber, &jog); commandhandler((char*)"GT_SetJogPrm", sRtn);
}

void SwitchAxisToJog(int axisNumber, double acc, double dec)
{
    short sRtn;
    TJogPrm jog;
    sRtn = GT_PrfJog(axisNumber);          commandhandler((char*)"GT_PrfJog", sRtn);
    sRtn = GT_GetJogPrm(axisNumber, &jog); commandhandler((char*)"GT_GetJogPrm", sRtn);
    jog.acc = acc; jog.dec = dec;
    sRtn = GT_SetJogPrm(axisNumber, &jog); commandhandler((char*)"GT_SetJogPrm", sRtn);
}

void PrintAxisStates(int count, const int axes[])
{
    long sts; double pos, vel;
    printf("\r");
    for (int i = 0; i < count; i++)
    {
        GT_GetSts(axes[i], &sts);
        GT_GetPrfPos(axes[i], &pos);
        GT_GetPrfVel(axes[i], &vel);
        printf("AXIS%-2d  sts=0x%lx  pos=%-10.1lf vel=%-10.1lf\n", axes[i], sts, pos, vel);
    }
    printf("\033[%dA", count);
}

short PrepareTrapMoveMultiAxes(const int axes[], const double relPulse[], int count, double acc, double dec, double smoothTime)
{
    short sRtn = 0;
    TTrapPrm trap;
    for (int i = 0; i < count; i++)
    {
        int ax = axes[i];
        sRtn = GT_PrfTrap(ax);            commandhandler((char*)"GT_PrfTrap", sRtn);
        sRtn = GT_GetTrapPrm(ax, &trap);  commandhandler((char*)"GT_GetTrapPrm", sRtn);
        trap.acc = acc; trap.dec = dec; trap.smoothTime = (short)smoothTime;
        sRtn = GT_SetTrapPrm(ax, &trap);  commandhandler((char*)"GT_SetTrapPrm", sRtn);

        double curPos = 0.0;
        sRtn = GT_GetPrfPos(ax, &curPos); commandhandler((char*)"GT_GetPrfPos", sRtn);
        const long targetPos = static_cast<long>(curPos + relPulse[i]);
        sRtn = GT_SetPos(ax, targetPos);  commandhandler((char*)"GT_SetPos", sRtn);
    }
    return sRtn;
}

void WaitAxesStop_0x400(const int axes[], int count)
{
    while (true)
    {
        bool moving = false;
        for (int i = 0; i < count; ++i)
        {
            long sts = 0;
            GT_GetSts(axes[i], &sts);
            if (sts & 0x400) { moving = true; break; }
        }
        if (!moving) break;
        Sleep(5);
    }
}

void ReturnAllAxesToInitialPosition()
{
    short sRtn;
    const int axes[5] = { axisNumber1, axisNumber2, axisNumber3, axisNumber4, axisNumber5 };
    printf("\n[BACK] Return all axes to initial position (PrfPos=0)...\n");
    sRtn = GT_Stop(0x1F, 0x1F); commandhandler((char*)"GT_Stop(All)", sRtn);
    Sleep(20);

    TTrapPrm trap;
    for (int i = 0; i < 5; ++i)
    {
        int ax = axes[i];
        sRtn = GT_PrfTrap(ax);            commandhandler((char*)"GT_PrfTrap", sRtn);
        sRtn = GT_GetTrapPrm(ax, &trap);  commandhandler((char*)"GT_GetTrapPrm", sRtn);
        trap.acc = jogacc; trap.dec = jogdec; trap.smoothTime = (short)smoothtime;
        sRtn = GT_SetTrapPrm(ax, &trap);  commandhandler((char*)"GT_SetTrapPrm", sRtn);
        sRtn = GT_SetPos(ax, 0L);         commandhandler((char*)"GT_SetPos(target=0)", sRtn);
        short vel = (ax == axisNumber5) ? rotationVelocity : bendingVelocity;
        sRtn = GT_SetVel(ax, vel);        commandhandler((char*)"GT_SetVel", sRtn);
    }
    sRtn = GT_Update(0x1F); commandhandler((char*)"GT_Update(1-5)", sRtn);
    WaitAxesStop_0x400(axes, 5);

    for (int i = 0; i < 5; ++i) SwitchAxisToJog(axes[i], jogacc, jogdec);
    printf("[BACK] Done. All axes at initial position.\n\n");
}

/* ==================== YOLO 跟踪执行 ==================== */
static void UpdateTrackingMotion(SharedVision* v)
{
    if (!v) return;

    LONG s1 = v->seq, s2 = v->seq;
    int guard = 0;
    while (s1 != s2 && ++guard < 5) { s1 = s2; s2 = v->seq; }
    if (s1 != s2) return;

    float dx = v->dx;
    float dy = v->dy;
    bool  locked = (v->locked != 0);

    int v13 = 0, v24 = 0;
    if (!locked)
    {
        if (dx > PIXEL_DEADZONE) v13 = +1;  // 需 dis3 增大（B 方向）
        else if (dx < -PIXEL_DEADZONE) v13 = -1;  // 需 dis3 减小（X 方向）
        if (dy > PIXEL_DEADZONE) v24 = +1;  // 需 dis4 增大（A 方向）
        else if (dy < -PIXEL_DEADZONE) v24 = -1;  // 需 dis4 减小（Y 方向）
    }

    // Apply the bend limit while allowing steps back toward the center.
    if (v13 != 0 && !CanBendStep(3, v13)) v13 = 0;
    if (v24 != 0 && !CanBendStep(4, v24)) v24 = 0;

    unsigned int m13 = (1u << (axisNumber1 - 1)) | (1u << (axisNumber3 - 1));
    unsigned int m24 = (1u << (axisNumber2 - 1)) | (1u << (axisNumber4 - 1));

    if (v13 != sLastV13)
    {
        if (v13 == 0) { GT_Stop(m13, m13); }
        else {
            GT_SetVel(axisNumber1, (short)(-v13 * bendingVelocity));
            GT_SetVel(axisNumber3, (short)(v13 * bendingVelocity));
            GT_Update(m13);
        }
        sLastV13 = v13;
    }

    if (v24 != sLastV24)
    {
        if (v24 == 0) { GT_Stop(m24, m24); }
        else {
            GT_SetVel(axisNumber2, (short)(-v24 * bendingVelocity));
            GT_SetVel(axisNumber4, (short)(v24 * bendingVelocity));
            GT_Update(m24);
        }
        sLastV24 = v24;
    }

    static DWORD lastPrint = 0;
    DWORD now = GetTickCount();
    if (now - lastPrint > 200)
    {
        lastPrint = now;
        double thetaDeg = GetCurrentBendLen() / ROD_RADIUS_MM * 180.0 / 3.141592653589793;
        printf("\r[TRK] dx=%+6.1f dy=%+6.1f locked=%d  bend=%5.2f°   ", dx, dy, (int)locked, thetaDeg);
    }
}

int main()
{
    short sRtn = 0;
    if (!InitSharedVision()) printf("[WARN] Shared vision memory unavailable.\n");

    if (GetControllerNum() > 0)
    {
        printf("One controller detected. System ready.\n");
        XINPUT_STATE state; ZeroMemory(&state, sizeof(XINPUT_STATE));

        sRtn = GT_Open();                         commandhandler((char*)"GT_Open", sRtn);
        sRtn = GT_Reset();                        commandhandler((char*)"GT_Reset", sRtn);
        char configPath[] = "GTS800.cfg";
        sRtn = GT_LoadConfig(configPath);          commandhandler((char*)"GT_LoadConfig", sRtn);
        sRtn = GT_ClrSts(1, 8);                   commandhandler((char*)"GT_ClrSts", sRtn);

        InitAxisJog(axisNumber1, jogacc, jogdec);
        InitAxisJog(axisNumber2, jogacc, jogdec);
        InitAxisJog(axisNumber3, jogacc, jogdec);
        InitAxisJog(axisNumber4, jogacc, jogdec);
        InitAxisJog(axisNumber5, jogacc, jogdec);
        InitAxisJog(axisNumber6, jogacc, jogdec);

        WORD lastButtons = 0;
        while (true)
        {
            DWORD dwResult = XInputGetState(0, &state);
            WORD  btn = state.Gamepad.wButtons;
            bool backEdge = (btn & XINPUT_GAMEPAD_BACK) && !(lastButtons & XINPUT_GAMEPAD_BACK);
            bool startEdge = (btn & XINPUT_GAMEPAD_START) && !(lastButtons & XINPUT_GAMEPAD_START);
            lastButtons = btn;

            bool bothTrig = (state.Gamepad.bLeftTrigger > TRIG_THRESHOLD) && (state.Gamepad.bRightTrigger > TRIG_THRESHOLD);
            if (bothTrig && !gLastBothTriggers)
            {
                gTrackingMode = !gTrackingMode;
                printf("\n[TRACKING] %s\n", gTrackingMode ? "ON (YOLO)" : "OFF (manual)");
                if (!gTrackingMode) GT_Stop(0x0F, 0x0F);
                sLastV13 = 0; sLastV24 = 0;
            }
            gLastBothTriggers = bothTrig;

            if (gTrackingMode)
            {
                if (backEdge)
                {
                    ReturnAllAxesToInitialPosition();
                    gTrackingMode = false; sLastV13 = 0; sLastV24 = 0;
                    continue;
                }
                UpdateTrackingMotion(gVision);
                Sleep(15);
                continue;
            }

            if (backEdge) { ReturnAllAxesToInitialPosition(); continue; }
            if (!IsControllerConnected()) { EmergencyStop(); break; }
            if (dwResult != ERROR_SUCCESS) continue;

            if (startEdge) {
                GT_SetVel(axisNumber6, axis6Velocity); GT_Update(1u << (axisNumber6 - 1));
                Sleep(5000);
                long m6 = 1L << (axisNumber6 - 1); GT_Stop(m6, m6);
                continue;
            }

            if (state.Gamepad.wButtons & XINPUT_GAMEPAD_B) {
                GT_SetVel(axisNumber1, -1 * bendingVelocity); GT_Update(1 << (axisNumber1 - 1));
                GT_SetVel(axisNumber3, bendingVelocity); GT_Update(1 << (axisNumber3 - 1));
                int ax[2] = { axisNumber1, axisNumber3 };
                while (true) {
                    XInputGetState(0, &state);
                    if (state.Gamepad.wButtons & XINPUT_GAMEPAD_BACK) { ReturnAllAxesToInitialPosition(); break; }
                    PrintAxisStates(2, ax);
                    if (!CanBendStep(3, +1)) {
                        GT_Stop((1 << (axisNumber1 - 1)) | (1 << (axisNumber3 - 1)), (1 << (axisNumber1 - 1)) | (1 << (axisNumber3 - 1))); break;
                    }
                    if (!(state.Gamepad.wButtons & XINPUT_GAMEPAD_B)) {
                        GT_Stop(1 << (axisNumber1 - 1), 1 << (axisNumber1 - 1));
                        GT_Stop(1 << (axisNumber3 - 1), 1 << (axisNumber3 - 1)); break;
                    }
                }
            }
            else if (state.Gamepad.wButtons & XINPUT_GAMEPAD_X) {
                GT_SetVel(axisNumber1, bendingVelocity); GT_Update(1 << (axisNumber1 - 1));
                GT_SetVel(axisNumber3, -1 * bendingVelocity); GT_Update(1 << (axisNumber3 - 1));
                int ax[2] = { axisNumber1, axisNumber3 };
                while (true) {
                    XInputGetState(0, &state);
                    if (state.Gamepad.wButtons & XINPUT_GAMEPAD_BACK) { ReturnAllAxesToInitialPosition(); break; }
                    PrintAxisStates(2, ax);
                    if (!CanBendStep(3, -1)) {
                        GT_Stop((1 << (axisNumber1 - 1)) | (1 << (axisNumber3 - 1)), (1 << (axisNumber1 - 1)) | (1 << (axisNumber3 - 1))); break;
                    }
                    if (!(state.Gamepad.wButtons & XINPUT_GAMEPAD_X)) {
                        GT_Stop(1 << (axisNumber1 - 1), 1 << (axisNumber1 - 1));
                        GT_Stop(1 << (axisNumber3 - 1), 1 << (axisNumber3 - 1)); break;
                    }
                }
            }
            else if (state.Gamepad.wButtons & XINPUT_GAMEPAD_Y) {
                GT_SetVel(axisNumber4, -1 * bendingVelocity); GT_Update(1 << (axisNumber4 - 1));
                GT_SetVel(axisNumber2, bendingVelocity); GT_Update(1 << (axisNumber2 - 1));
                int ax[2] = { axisNumber2, axisNumber4 };
                while (true) {
                    XInputGetState(0, &state);
                    if (state.Gamepad.wButtons & XINPUT_GAMEPAD_BACK) { ReturnAllAxesToInitialPosition(); break; }
                    PrintAxisStates(2, ax);
                    if (!CanBendStep(4, -1)) {
                        GT_Stop((1 << (axisNumber2 - 1)) | (1 << (axisNumber4 - 1)), (1 << (axisNumber2 - 1)) | (1 << (axisNumber4 - 1))); break;
                    }
                    if (!(state.Gamepad.wButtons & XINPUT_GAMEPAD_Y)) {
                        GT_Stop(1 << (axisNumber4 - 1), 1 << (axisNumber4 - 1));
                        GT_Stop(1 << (axisNumber2 - 1), 1 << (axisNumber2 - 1)); break;
                    }
                }
            }
            else if (state.Gamepad.wButtons & XINPUT_GAMEPAD_A) {
                GT_SetVel(axisNumber4, bendingVelocity); GT_Update(1 << (axisNumber4 - 1));
                GT_SetVel(axisNumber2, -1 * bendingVelocity); GT_Update(1 << (axisNumber2 - 1));
                int ax[2] = { axisNumber2, axisNumber4 };
                while (true) {
                    XInputGetState(0, &state);
                    if (state.Gamepad.wButtons & XINPUT_GAMEPAD_BACK) { ReturnAllAxesToInitialPosition(); break; }
                    PrintAxisStates(2, ax);
                    if (!CanBendStep(4, +1)) {
                        GT_Stop((1 << (axisNumber2 - 1)) | (1 << (axisNumber4 - 1)), (1 << (axisNumber2 - 1)) | (1 << (axisNumber4 - 1))); break;
                    }
                    if (!(state.Gamepad.wButtons & XINPUT_GAMEPAD_A)) {
                        GT_Stop(1 << (axisNumber4 - 1), 1 << (axisNumber4 - 1));
                        GT_Stop(1 << (axisNumber2 - 1), 1 << (axisNumber2 - 1)); break;
                    }
                }
            }
            else if (state.Gamepad.wButtons & XINPUT_GAMEPAD_DPAD_RIGHT) {
                GT_SetVel(axisNumber5, rotationVelocity); GT_Update(1 << (axisNumber5 - 1));
                int ax[1] = { axisNumber5 };
                while (true) {
                    XInputGetState(0, &state);
                    if (state.Gamepad.wButtons & XINPUT_GAMEPAD_BACK) { ReturnAllAxesToInitialPosition(); break; }
                    PrintAxisStates(1, ax);
                    if (!(state.Gamepad.wButtons & XINPUT_GAMEPAD_DPAD_RIGHT)) {
                        GT_Stop(1 << (axisNumber5 - 1), 1 << (axisNumber5 - 1)); break;
                    }
                }
            }
            else if (state.Gamepad.wButtons & XINPUT_GAMEPAD_DPAD_LEFT) {
                GT_SetVel(axisNumber5, -1 * rotationVelocity); GT_Update(1 << (axisNumber5 - 1));
                int ax[1] = { axisNumber5 };
                while (true) {
                    XInputGetState(0, &state);
                    if (state.Gamepad.wButtons & XINPUT_GAMEPAD_BACK) { ReturnAllAxesToInitialPosition(); break; }
                    PrintAxisStates(1, ax);
                    if (!(state.Gamepad.wButtons & XINPUT_GAMEPAD_DPAD_LEFT)) {
                        GT_Stop(1 << (axisNumber5 - 1), 1 << (axisNumber5 - 1)); break;
                    }
                }
            }
            else if (state.Gamepad.wButtons & XINPUT_GAMEPAD_LEFT_SHOULDER) {
                while (true) {
                    double p3, p4; GT_GetPrfPos(axisNumber3, &p3); GT_GetPrfPos(axisNumber4, &p4);
                    double dis3 = p3 / PULSE_PER_MM, dis4 = p4 / PULSE_PER_MM;
                    double r = ROD_RADIUS_MM, len = std::sqrt(dis3 * dis3 + dis4 * dis4);
                    if (len < 1e-6) break;

                    double alpha = std::atan2(dis4, dis3);
                    double theta = len / r;
                    double beta = selfRotationAngle * 3.141592653589793 / 180.0;

                    double l3 = r * theta * (cos(alpha) * cos(beta) - sin(alpha) * sin(beta) - cos(alpha));
                    double l4 = r * theta * (sin(alpha) * cos(beta) + cos(alpha) * sin(beta) - sin(alpha));
                    double l1 = -l3, l2 = -l4;

                    double newLen = std::sqrt((dis3 + l3) * (dis3 + l3) + (dis4 + l4) * (dis4 + l4));
                    if (newLen > MAX_BEND_LEN_MM) {
                        printf("\n[LB] Bend limit would be exceeded, skip.\n"); break;
                    }

                    double p_l1 = l1 * PULSE_PER_MM, p_l3 = l3 * PULSE_PER_MM;
                    double p_l4 = l4 * PULSE_PER_MM, p_l2 = l2 * PULSE_PER_MM;
                    double p_l5 = -(selfRotationAngle / 360.0) * (100.0 / 40.0) * 123000.0;

                    int axes[5] = { axisNumber1, axisNumber2, axisNumber3, axisNumber4, axisNumber5 };
                    double relPulse[5] = { p_l1, p_l2, p_l3, p_l4, p_l5 };
                    PrepareTrapMoveMultiAxes(axes, relPulse, 5, jogacc, jogdec, smoothtime);

                    unsigned int mask = 0;
                    for (int i = 0; i < 5; i++) {
                        short vel = (axes[i] == axisNumber5) ? rotationVelocity : bendingVelocity;
                        GT_SetVel(axes[i], vel); mask |= (1u << (axes[i] - 1));
                    }
                    GT_Update(mask);

                    long sts1, sts2, sts3, sts4, sts5;
                    while (true) {
                        GT_GetSts(axisNumber1, &sts1); GT_GetSts(axisNumber2, &sts2);
                        GT_GetSts(axisNumber3, &sts3); GT_GetSts(axisNumber4, &sts4);
                        GT_GetSts(axisNumber5, &sts5);
                        if (!((sts1 & 0x400) || (sts2 & 0x400) || (sts3 & 0x400) || (sts4 & 0x400) || (sts5 & 0x400))) break;
                        Sleep(5);
                    }
                    XInputGetState(0, &state);
                    if (!(state.Gamepad.wButtons & XINPUT_GAMEPAD_LEFT_SHOULDER)) break;
                }
                for (int i = 0; i < 5; ++i) SwitchAxisToJog(axisNumber1 + i, jogacc, jogdec);
            }
            else if (state.Gamepad.wButtons & XINPUT_GAMEPAD_RIGHT_SHOULDER) {
                while (true) {
                    double p3, p4; GT_GetPrfPos(axisNumber3, &p3); GT_GetPrfPos(axisNumber4, &p4);
                    double dis3 = p3 / PULSE_PER_MM, dis4 = p4 / PULSE_PER_MM;
                    double r = ROD_RADIUS_MM, len = std::sqrt(dis3 * dis3 + dis4 * dis4);
                    if (len < 1e-6) break;

                    double alpha = std::atan2(dis4, dis3);
                    double theta = len / r;
                    double beta = -selfRotationAngle * 3.141592653589793 / 180.0;

                    double l3 = r * theta * (cos(alpha) * cos(beta) - sin(alpha) * sin(beta) - cos(alpha));
                    double l4 = r * theta * (sin(alpha) * cos(beta) + cos(alpha) * sin(beta) - sin(alpha));
                    double l1 = -l3, l2 = -l4;

                    double newLen = std::sqrt((dis3 + l3) * (dis3 + l3) + (dis4 + l4) * (dis4 + l4));
                    if (newLen > MAX_BEND_LEN_MM) {
                        printf("\n[RB] Bend limit would be exceeded, skip.\n"); break;
                    }

                    double p_l1 = l1 * PULSE_PER_MM, p_l3 = l3 * PULSE_PER_MM;
                    double p_l4 = l4 * PULSE_PER_MM, p_l2 = l2 * PULSE_PER_MM;
                    double p_l5 = (selfRotationAngle / 360.0) * (100.0 / 40.0) * 123000.0;

                    int axes[5] = { axisNumber1, axisNumber2, axisNumber3, axisNumber4, axisNumber5 };
                    double relPulse[5] = { p_l1, p_l2, p_l3, p_l4, p_l5 };
                    PrepareTrapMoveMultiAxes(axes, relPulse, 5, jogacc, jogdec, smoothtime);

                    unsigned int mask = 0;
                    for (int i = 0; i < 5; i++) {
                        short vel = (axes[i] == axisNumber5) ? rotationVelocity : bendingVelocity;
                        GT_SetVel(axes[i], vel); mask |= (1u << (axes[i] - 1));
                    }
                    GT_Update(mask);

                    long sts1, sts2, sts3, sts4, sts5;
                    while (true) {
                        GT_GetSts(axisNumber1, &sts1); GT_GetSts(axisNumber2, &sts2);
                        GT_GetSts(axisNumber3, &sts3); GT_GetSts(axisNumber4, &sts4);
                        GT_GetSts(axisNumber5, &sts5);
                        if (!((sts1 & 0x400) || (sts2 & 0x400) || (sts3 & 0x400) || (sts4 & 0x400) || (sts5 & 0x400))) break;
                        Sleep(5);
                    }
                    XInputGetState(0, &state);
                    if (!(state.Gamepad.wButtons & XINPUT_GAMEPAD_RIGHT_SHOULDER)) break;
                }
                for (int i = 0; i < 5; ++i) SwitchAxisToJog(axisNumber1 + i, jogacc, jogdec);
            }
            else if (_kbhit()) break;
        }

        GT_AxisOff(1); GT_AxisOff(2); GT_AxisOff(3); GT_AxisOff(4); GT_AxisOff(5); GT_AxisOff(6);
    }
    else printf("no controllor connected\n");

    CloseSharedVision();
    return 0;
}
