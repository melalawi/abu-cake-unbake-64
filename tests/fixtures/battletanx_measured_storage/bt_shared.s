.include "macro.inc"

.set noat
.set noreorder
.set gp=64

.section .text, "ax"

.globl func_800949D8_us
.ent func_800949D8_us
func_800949D8_us:
      addiu      $sp, $sp, -0x38
      lw         $t5, 0x48($sp)
      lw         $t8, 0x4C($sp)
      lw         $t7, 0x50($sp)
      subu       $t9, $a2, $a0
      bnez       $t9, .L80094A50
       addiu     $t3, $zero, 0x1
      blez       $t5, .L80094C4C
       addu      $t1, $zero, $zero
      sll        $a0, $a0, 3
      lui        $a3, %hi(D_802D5BC0)
      addiu      $a3, $a3, %lo(D_802D5BC0)
      sll        $v1, $t7, 2
      addu       $v1, $v1, $t7
      sll        $v0, $v1, 4
      subu       $v0, $v0, $v1
      sll        $a2, $v0, 5
  .L80094A1C:
      addu       $v1, $a0, $a2
      addu       $v1, $v1, $a3
      lw         $v0, 0x0($v1)
      slt        $v0, $a1, $v0
      beqz       $v0, .L80094A3C
       addiu     $t1, $t1, 0x1
      sw         $a1, 0x0($v1)
      sw         $t8, 0x4($v1)
  .L80094A3C:
      slt        $v0, $t1, $t5
      bnez       $v0, .L80094A1C
       addiu     $a2, $a2, 0x320
      j          .L80094C4C
       nop
  .L80094A50:
      blez       $t5, .L80094AC0
       addu      $t1, $zero, $zero
      sll        $v1, $t7, 2
      addu       $v1, $v1, $t7
      sll        $v0, $v1, 4
      subu       $v0, $v0, $v1
      sll        $t6, $v0, 5
      sll        $t4, $a0, 3
      addu       $t2, $sp, $zero
      lui        $t0, %hi(D_802D5BC0)
      addiu      $t0, $t0, %lo(D_802D5BC0)
  .L80094A7C:
      addu       $v0, $t6, $t0
      addu       $v1, $v0, $t4
      lw         $v0, 0x0($v1)
      slt        $v0, $a1, $v0
      beqz       $v0, .L80094AA4
       nop
      addu       $t3, $zero, $zero
      sw         $a1, 0x0($v1)
      j          .L80094AAC
       sw        $t8, 0x4($v1)
  .L80094AA4:
      lw         $v0, 0x4($v1)
      sw         $v0, 0x0($t2)
  .L80094AAC:
      addiu      $t2, $t2, 0x4
      addiu      $t1, $t1, 0x1
      slt        $v0, $t1, $t5
      bnez       $v0, .L80094A7C
       addiu     $t0, $t0, 0x320
  .L80094AC0:
      blez       $t5, .L80094B30
       addu      $t1, $zero, $zero
      sll        $v1, $t7, 2
      addu       $v1, $v1, $t7
      sll        $v0, $v1, 4
      subu       $v0, $v0, $v1
      sll        $t6, $v0, 5
      sll        $t4, $a2, 3
      addu       $t2, $sp, $zero
      lui        $t0, %hi(D_802D5BC0)
      addiu      $t0, $t0, %lo(D_802D5BC0)
  .L80094AEC:
      addu       $v0, $t6, $t0
      addu       $v1, $v0, $t4
      lw         $v0, 0x0($v1)
      slt        $v0, $a3, $v0
      beqz       $v0, .L80094B14
       nop
      addu       $t3, $zero, $zero
      sw         $a3, 0x0($v1)
      j          .L80094B1C
       sw        $t8, 0x4($v1)
  .L80094B14:
      lw         $v0, 0x4($v1)
      sw         $v0, 0xC($t2)
  .L80094B1C:
      addiu      $t2, $t2, 0x4
      addiu      $t1, $t1, 0x1
      slt        $v0, $t1, $t5
      bnez       $v0, .L80094AEC
       addiu     $t0, $t0, 0x320
  .L80094B30:
      beqz       $t3, .L80094B88
       sltu      $v1, $zero, $t3
      addu       $t1, $zero, $zero
      slt        $v0, $t1, $t5
      and        $v0, $v0, $v1
      beqz       $v0, .L80094B80
       nop
      addu       $t0, $sp, $zero
  .L80094B50:
      lw         $v0, 0x0($t0)
      lw         $v1, 0xC($t0)
      addiu      $t1, $t1, 0x1
      xor        $v0, $v0, $v1
      sltiu      $v0, $v0, 0x1
      negu       $v0, $v0
      and        $t3, $t3, $v0
      slt        $v0, $t1, $t5
      sltu       $v1, $zero, $t3
      and        $v0, $v0, $v1
      bnez       $v0, .L80094B50
       addiu     $t0, $t0, 0x4
  .L80094B80:
      bnez       $t3, .L80094C4C
       nop
  .L80094B88:
      addiu      $t3, $a0, 0x1
      addiu      $v0, $a2, -0x1
      slt        $v0, $v0, $t3
      bnez       $v0, .L80094C4C
       subu      $a3, $a3, $a1
      sll        $v1, $t7, 2
      addu       $v1, $v1, $t7
      sll        $v0, $v1, 4
      subu       $v0, $v0, $v1
      sll        $t7, $v0, 5
      lui        $t6, %hi(D_802D5BC0)
      addiu      $t6, $t6, %lo(D_802D5BC0)
      subu       $v0, $t3, $a0
  .L80094BBC:
      mult       $v0, $a3
      mflo       $v0
      nop
      nop
      div        $v0, $t9
      bnez       $t9, .L80094BDC
       nop
      .word      0x0007000D                    # break      7 # 00000000 <InstrIdType: CPU_SPECIAL>
  .L80094BDC:
      addiu      $at, $zero, -0x1
      bne        $t9, $at, .L80094BF4
       lui       $at, (0x80000000 >> 16)
      bne        $v0, $at, .L80094BF4
       nop
      .word      0x0006000D                    # break      6 # 00000000 <InstrIdType: CPU_SPECIAL>
  .L80094BF4:
      mflo       $v0
      addu       $t1, $zero, $zero
      blez       $t5, .L80094C38
       addu      $t2, $a1, $v0
      sll        $t4, $t3, 3
      addu       $t0, $t7, $zero
  .L80094C0C:
      addu       $v1, $t4, $t0
      addu       $v1, $v1, $t6
      lw         $v0, 0x0($v1)
      slt        $v0, $t2, $v0
      beqz       $v0, .L80094C2C
       addiu     $t1, $t1, 0x1
      sw         $t2, 0x0($v1)
      sw         $t8, 0x4($v1)
  .L80094C2C:
      slt        $v0, $t1, $t5
      bnez       $v0, .L80094C0C
       addiu     $t0, $t0, 0x320
  .L80094C38:
      addiu      $t3, $t3, 0x1
      addiu      $v0, $a2, -0x1
      slt        $v0, $v0, $t3
      beqz       $v0, .L80094BBC
       subu      $v0, $t3, $a0
  .L80094C4C:
      addiu      $sp, $sp, 0x38
      jr         $ra
       nop
.end func_800949D8_us
