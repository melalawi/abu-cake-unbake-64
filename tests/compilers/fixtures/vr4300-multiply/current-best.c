#include "types.h"
#include "span_1000/code_80114520.h"
#include "span_1000/code_801113A0.h"

extern f32 func_8011BE10_us(f32);

/* The cartridge uses float[4][4], a halfword output, and five single precision
 * inputs. The degree conversion constant is the exact double at 80075A00.
 * No compiler or generated declaration changes are part of this candidate. */
void func_80114970_us(f32 m[4][4], u16 *norm, f32 fovy,
                     f32 aspect, f32 near, f32 far, f32 scale)
{
    f32 cot;
    s32 i;
    s32 j;

    func_801147A0(m);
    fovy = (f32)((f64)fovy * 0.017453292222222222);
    cot = func_801113A0_us(fovy / 2) / func_8011BE10_us(fovy / 2);
    m[0][0] = cot / aspect;
    m[1][1] = cot;
    m[2][2] = (near + far) / (near - far);
    m[2][3] = -1.0f;
    m[3][2] = (2 * near * far) / (near - far);
    m[3][3] = 0.0f;
    for (i = 0; i < 4; i++) {
        for (j = 0; j < 4; j++) {
            m[i][j] = m[i][j] * scale;
        }
    }
    if (norm != 0) {
        if ((f64)(near + far) <= 2.0) {
            *norm = 0xFFFF;
        } else {
            *norm = (u32)(131072.0 / (f64)(near + far));
            if (*norm <= 0) {
                *norm = 1;
            }
        }
    }
}
