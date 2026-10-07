#include "common/unused.h"
#include "types.h"
struct PlayerViewConstants {
    f32 yawScale;
    f32 eyeHeight;
};
extern const struct PlayerViewConstants D_800C2D40_de;

#include "common/types_06e4f7ef1f9e.h"
#include "span_1000/code_8022BA90.h"

extern int D_80140FC0;
extern int D_800CD72C;
extern void func_80226DD0_de(Player1680 *, Matrix *);
extern void func_802732D0_de(Matrix *, Vec3 *);
extern void func_802727D8_de(Matrix *);
extern void func_802736D4_de(Matrix *, float);
extern void func_80273448_de(Matrix *, float, float, float);
extern void func_8027027C_de(Matrix *, void *);

static inline void getEye(Player1680 *player, Vec3 *pos) {
    Matrix frame;

    if (player->mount != 0) {
        *pos = player->mount->unk128;
    } else {
        func_80226DD0_de(player, &frame);
        func_802732D0_de(&frame, pos);
    }
}

static inline float getYaw(void) {
    return D_80140FC0 * D_800C2D40_de.yawScale;
}

void func_8022C490_de(Player1680 *player) {
    Matrix mtx;
    Vec3 pos;

    getEye(player, &pos);
    func_802727D8_de(&mtx);
    func_802736D4_de(&mtx, getYaw());
    func_80273448_de(&mtx, pos.x, pos.y + D_800C2D40_de.eyeHeight, pos.z);
    func_8027027C_de(&mtx, &player->views[D_800CD72C]);
}
