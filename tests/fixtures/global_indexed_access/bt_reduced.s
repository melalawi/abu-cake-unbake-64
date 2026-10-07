    /* A2EBC 80112EBC 8FAB0030 */  lw         $t3, 0x30($sp)
    /* A2EC0 80112EC0 3C0E8014 */  lui        $t6, %hi(D_80145DE0)
    /* A2EC4 80112EC4 916C0009 */  lbu        $t4, 0x9($t3)
    /* A2EC8 80112EC8 000C6880 */  sll        $t5, $t4, 2
    /* A2ECC 80112ECC 01CD7021 */  addu       $t6, $t6, $t5
    /* A2ED0 80112ED0 8DCE5DE0 */  lw         $t6, %lo(D_80145DE0)($t6)
    /* A2ED4 80112ED4 AFAC0028 */  sw         $t4, 0x28($sp)
    /* A2FE8 80112FE8 8FAE0030 */  lw         $t6, 0x30($sp)
    /* A2FEC 80112FEC 3C018014 */  lui        $at, %hi(D_80145DE0)
    /* A2FF0 80112FF0 000F5880 */  sll        $t3, $t7, 2
    /* A2FF4 80112FF4 002B0821 */  addu       $at, $at, $t3
    /* A2FF8 80112FF8 AC2E5DE0 */  sw         $t6, %lo(D_80145DE0)($at)
