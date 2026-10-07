
/* func_802023C4_de: types.declaration: solved callee prototype */
extern int func_802023C4_de(void);
/* func_802023D0_de: types.declaration: solved callee prototype */
extern void func_802023D0_de(int arg0);

                                                  /* size = 0x18 */

s32 func_80200500_de(void) {
    s32 temp_v0;

    temp_v0 = func_802023C4_de();
    func_802023D0_de(temp_v0 & ~1);
    return temp_v0 & 1;
}

/* func_802023C4_de: types.declaration: solved callee prototype */
extern int func_802023C4_de(void);
/* func_802023D0_de: types.declaration: solved callee prototype */
extern void func_802023D0_de(int arg0);

                                                  /* size = 0x18 */

void func_80200538_de(s32 arg0, s32 arg1) {
    func_802023D0_de(func_802023C4_de() | arg0);
}
