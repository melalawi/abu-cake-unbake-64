void func_8011DFE0_us(struct Shape_func_8011DC98 *arg0, void *arg1) {
    func_8011E564_S1_Shared8011E564 *var_s3;
    s32 temp_s7;
    M2C_FIELD(arg0, s32 (**)(void *), 0x24) = (s32 (*)(void *)) M2C_FIELD(arg1, s32 (**)(void *), 0x10);
            func_8011E564(var_s3, M2C_FIELD(arg0, s32 (**)(void *), 0x24), temp_s7);
}
