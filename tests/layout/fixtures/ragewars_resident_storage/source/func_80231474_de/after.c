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
