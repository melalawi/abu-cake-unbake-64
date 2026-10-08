#include "types.h"

/* 802A7AA4 reads word opcodes then signed integer position operands.
 * Opcode 1 sets x/y; opcode 3 moves to x/y in the supplied frame count.
 * Opcode zero stops parsing through the default dispatch case.
 * 802AA730 and 802AA760 install the two named origin scripts.
 * ROM D3D6C..D3DC8. */
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
