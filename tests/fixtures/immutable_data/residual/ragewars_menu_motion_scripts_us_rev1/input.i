typedef signed char s8;
typedef unsigned char u8;
typedef signed short s16;
typedef unsigned short u16;
typedef signed int s32;
typedef unsigned int u32;
typedef signed long long s64;
typedef unsigned long long u64;
typedef float f32;
typedef double f64;
enum ResidentMenuMotionOpcode {
    MENU_MOTION_STOP = 0,
    MENU_MOTION_POSITION = 1,
    MENU_MOTION_MOVE = 3
};
struct ResidentMenuPositionScript {
    s32 opcode;
    s32 x;
    s32 y;
    s32 stop;
};
struct ResidentMenuMoveScript {
    s32 opcode;
    s32 x;
    s32 y;
    s32 frames;
    s32 stop;
};
struct ResidentMenuPositionScript D_800D316C = {MENU_MOTION_POSITION, 0, -40, MENU_MOTION_STOP};
struct ResidentMenuPositionScript D_800D317C = {MENU_MOTION_POSITION, 0, -20, MENU_MOTION_STOP};
struct ResidentMenuMoveScript D_800CDEB4 = {MENU_MOTION_MOVE, 0, 0, 2, MENU_MOTION_STOP};
struct ResidentMenuMoveScript D_800CDEC8 = {MENU_MOTION_MOVE, 0, 0, 2, MENU_MOTION_STOP};
struct ResidentMenuMoveScript D_800D31B4 = {MENU_MOTION_MOVE, 0, -40, 2, MENU_MOTION_STOP};
