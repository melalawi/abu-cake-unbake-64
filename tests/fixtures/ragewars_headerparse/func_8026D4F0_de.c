void func_8026D4F0_de(void **resource, s32 unused, s32 matrix, s32 segment, s32 lights, void *textures, s32 pass, u8 r, u8 g, u8 b, u8 a) {
#if defined(VERSION_DE)
    if (D_800DE854 - ((u32)D_8010C574 - (u32)D_8011BDC0->commands) / sizeof(Gfx) < 3000) {
#elif defined(VERSION_EU)
    if (D_800EEEC4 - ((u32)D_8010C574 - (u32)D_8011BDC0->commands) / sizeof(Gfx) < 3000) {
#elif defined(VERSION_EU_X)
    if (D_800EA084 - ((u32)D_8010C574 - (u32)D_8011BDC0->commands) / sizeof(Gfx) < 3000) {
#elif defined(VERSION_US)
    if (D_800DD504 - ((u32)D_8010C574 - (u32)D_8011BDC0->commands) / sizeof(Gfx) < 3000) {
#elif defined(VERSION_US_REV1)
    if (D_800E28A4 - ((u32)D_8010C574 - (u32)D_8011BDC0->commands) / sizeof(Gfx) < 3000) {
#endif
        return;
    }
}

extern void func_8026AD8C_de(s32 arg0, s32 arg1, s32 arg2, s32 arg3);

void func_8026D7D4_de(s32 arg0, s32 arg1) {
    func_8026AD8C_de(arg0, arg1, 0, 0);
}

extern void func_8026AD8C_de(s32 arg0, s32 arg1, s32 arg2, s32 arg3);

void func_8026D7F4_de(s32 arg0, s32 arg1) {
    func_8026AD8C_de(arg0, arg1, 0, 1);
}

extern void func_8026AD8C_de(s32 arg0, s32 arg1, s32 arg2, s32 arg3);

void func_8026D814_de(s32 arg0, s32 arg1) {
    func_8026AD8C_de(arg0, arg1, 1, 0);
}

void func_8026D834_de(void) {
}
#ifndef VERSION_US_REV1
void func_8026D83C_de(void) {
}
#endif
