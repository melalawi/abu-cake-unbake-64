#include "span_1000/code_8020FDB0.h"
#include "span_1000/code_8022D944.h"
#include "common/types_06e4f7ef1f9e.h"
#include "types.h"

extern float D_800C2B70_de;
extern f32 D_80111D2C;
extern float func_802B7130_de(float x);
extern float func_802B6560_de(float x);
extern void func_80274098_de(void *arg0, void *arg1, void *arg2);

void func_80226C60_de(void *object, Vector4f *output)
{
    Vector4f pitchRotation;
    Vector4f yawRotation;
    Vector4f rollRotation;
    Shared_Quad orientation;
    Vector4f pitchYaw;
    Vector4f oriented;
    SharedPlayer_func_8020FDB0_de *player = object;
    float pitch;
    float roll;
    float heading;
    float yaw;
    float scale;

    pitch = player->views5E8.view724_71.unk724 + player->views5E8.view730_78.unk730[0];
    roll = player->views5E8.view72C_75.unk72C + player->views5E8.view730_78.unk730[2];
    scale = D_800C2B70_de;
    roll *= scale;
    yaw = player->views5E8.view728_73.unk728 + player->views5E8.view730_78.unk730[1];
    heading = player->views1C.view6C_4.unk6C;
    orientation = player->views1C.view5C_6.unk5C;
    pitch -= ((func_8022DBD4_S1 *)object)->unk708;

    D_80111D2C = func_802B7130_de(roll);
    pitch *= scale;
    rollRotation.x = 0.0f;
    rollRotation.y = 0.0f;
    rollRotation.z = D_80111D2C;
    rollRotation.w = func_802B6560_de(roll);

    D_80111D2C = func_802B7130_de(pitch);
    pitchRotation.x = D_80111D2C;
    pitchRotation.y = 0.0f;
    pitchRotation.z = 0.0f;
    pitchRotation.w = func_802B6560_de(pitch);

    heading = (heading + yaw) * scale;
    D_80111D2C = func_802B7130_de(heading);
    yawRotation.x = 0.0f;
    yawRotation.y = D_80111D2C;
    yawRotation.z = 0.0f;
    yawRotation.w = func_802B6560_de(heading);

    func_80274098_de(&pitchYaw, &pitchRotation, &yawRotation);
    func_80274098_de(&oriented, &pitchYaw, &orientation);
    func_80274098_de(output, &rollRotation, &oriented);
}
