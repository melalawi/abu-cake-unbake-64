#include "span_1000/code_8022F3E8.h"
#include "span_C76B0/data.h"
#include "types.h"
#include "common/types_06e4f7ef1f9e.h"

extern f32 D_800CD738;
extern f32 func_802747A0_de(f32, f32);
extern void func_80274870_de(f32 *, f32, f32);

void func_80231474_de(Shared_Actor *arg0, WeaponFireState *arg1) {
    f32 temp_f12;
    f32 temp_f1;
    f32 temp_f2;
    SharedPlayer *temp_s1;
    void *temp_v0;

    temp_s1 = (SharedPlayer *)arg0->entity;
    if (arg1->variant != 4) {
        temp_f12 = arg1->spin;
        if (temp_f12 > 0.0f) {
            arg1->spin = func_802747A0_de(temp_f12, D_800C2F68_de);
        } else {
            f32 value = arg1->spinStep;
            f32 period = D_800C2F6C_de;
            arg1->spin = 0.0f;
            if (period <= value) {
                do {
                    value -= period;
                    arg1->spinStep = value;
                } while (period <= value);
            }
            period = arg1->spinStep;
            if (period < D_800C2F70_de) {
                func_80274870_de(&arg1->spinStep, 0.0f, 0.125f);
            } else if (period < D_800C2F74_de) {
                func_80274870_de(&arg1->spinStep, 2.0943952f, 0.125f);
            } else if (period < D_800C2F78_de) {
                func_80274870_de(&arg1->spinStep, 4.1887903f, 0.125f);
            } else {
                func_80274870_de(&arg1->spinStep, 6.2831855f, 0.125f);
            }
        }
    }
    arg1->spinStep += (arg1->spin * D_800CD738) * 2.0f;
    temp_f1 = arg1->spin;
    temp_v0 = temp_s1->views5E8.view698_34.unk698;
    if (!(temp_f1 < 0.0f
              ? D_800C2F80_de < (-temp_f1 * D_800C2F7C_de)
              : D_800C2F88_de < (temp_f1 * D_800C2F84_de))) {
        temp_f2 = arg1->spin;
        if (temp_f2 < 0.0f) {
            ((WeaponAnimationState *)temp_v0)->speed = (f32) (-temp_f2 * D_800C2F8C_de);
            return;
        }
        ((WeaponAnimationState *)temp_v0)->speed = (f32) (temp_f2 * D_800C2F90_de);
        return;
    }
    ((WeaponAnimationState *)temp_v0)->speed = (f32) D_800C2F94_de;
}

/* Native resident constant storage; absolute access symbols retain their addresses. */
#if defined(VERSION_US)
const float unbake_rodata_800C2E98_4 = 0.0136135686f;
const float unbake_rodata_800C2E9C_4 = 6.28318548f;
const float unbake_rodata_800C2EA0_4 = 1.04719758f;
const float unbake_rodata_800C2EA4_4 = 3.14159274f;
const float unbake_rodata_800C2EA8_4 = 5.23598766f;
const float unbake_rodata_800C2EAC_4 = 1.79049289f;
const float unbake_rodata_800C2EB0_4 = 1.0f;
const float unbake_rodata_800C2EB4_4 = 1.79049289f;
const float unbake_rodata_800C2EB8_4 = 1.0f;
const float unbake_rodata_800C2EBC_4 = 1.79049289f;
const float unbake_rodata_800C2EC0_4 = 1.79049289f;
const float unbake_rodata_800C2EC4_4 = 1.0f;
#elif defined(VERSION_US_REV1)
const float unbake_rodata_800C8058_4 = 0.0136135686f;
const float unbake_rodata_800C805C_4 = 6.28318548f;
const float unbake_rodata_800C8060_4 = 1.04719758f;
const float unbake_rodata_800C8064_4 = 3.14159274f;
const float unbake_rodata_800C8068_4 = 5.23598766f;
const float unbake_rodata_800C806C_4 = 1.79049289f;
const float unbake_rodata_800C8070_4 = 1.0f;
const float unbake_rodata_800C8074_4 = 1.79049289f;
const float unbake_rodata_800C8078_4 = 1.0f;
const float unbake_rodata_800C807C_4 = 1.79049289f;
const float unbake_rodata_800C8080_4 = 1.79049289f;
const float unbake_rodata_800C8084_4 = 1.0f;
#elif defined(VERSION_EU)
const float unbake_rodata_800C3218_4 = 0.0136135686f;
const float unbake_rodata_800C321C_4 = 6.28318548f;
const float unbake_rodata_800C3220_4 = 1.04719758f;
const float unbake_rodata_800C3224_4 = 3.14159274f;
const float unbake_rodata_800C3228_4 = 5.23598766f;
const float unbake_rodata_800C322C_4 = 1.79049289f;
const float unbake_rodata_800C3230_4 = 1.0f;
const float unbake_rodata_800C3234_4 = 1.79049289f;
const float unbake_rodata_800C3238_4 = 1.0f;
const float unbake_rodata_800C323C_4 = 1.79049289f;
const float unbake_rodata_800C3240_4 = 1.79049289f;
const float unbake_rodata_800C3244_4 = 1.0f;
#elif defined(VERSION_EU_X)
const float unbake_rodata_800C3258_4 = 0.0136135686f;
const float unbake_rodata_800C325C_4 = 6.28318548f;
const float unbake_rodata_800C3260_4 = 1.04719758f;
const float unbake_rodata_800C3264_4 = 3.14159274f;
const float unbake_rodata_800C3268_4 = 5.23598766f;
const float unbake_rodata_800C326C_4 = 1.79049289f;
const float unbake_rodata_800C3270_4 = 1.0f;
const float unbake_rodata_800C3274_4 = 1.79049289f;
const float unbake_rodata_800C3278_4 = 1.0f;
const float unbake_rodata_800C327C_4 = 1.79049289f;
const float unbake_rodata_800C3280_4 = 1.79049289f;
const float unbake_rodata_800C3284_4 = 1.0f;
#elif defined(VERSION_DE)
const float unbake_rodata_800C2F68_4 = 0.0136135686f;
const float unbake_rodata_800C2F6C_4 = 6.28318548f;
const float unbake_rodata_800C2F70_4 = 1.04719758f;
const float unbake_rodata_800C2F74_4 = 3.14159274f;
const float unbake_rodata_800C2F78_4 = 5.23598766f;
const float unbake_rodata_800C2F7C_4 = 1.79049289f;
const float unbake_rodata_800C2F80_4 = 1.0f;
const float unbake_rodata_800C2F84_4 = 1.79049289f;
const float unbake_rodata_800C2F88_4 = 1.0f;
const float unbake_rodata_800C2F8C_4 = 1.79049289f;
const float unbake_rodata_800C2F90_4 = 1.79049289f;
const float unbake_rodata_800C2F94_4 = 1.0f;
#endif
