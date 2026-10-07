/* Real word-copy assignments, with address-only test parameters. */
void func_80113E10(void *var_t6, void *var_t7, void *var_t8, void *var_t9) {
    M2C_FIELD(var_t8, s32 *, -8) = (s32) M2C_UNALIGNED32(M2C_FIELD(var_t9, M2C_UNK *, -8));
    M2C_FIELD(var_t8, s32 *, -4) = (s32) M2C_UNALIGNED32(M2C_FIELD(var_t9, M2C_UNK *, -4));
    M2C_FIELD(var_t8, s32 *, 0) = (s32) M2C_UNALIGNED32(M2C_FIELD(var_t9, M2C_UNK *, 0));
}
