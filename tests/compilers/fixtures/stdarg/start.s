.text
.set noreorder
.globl _start
_start:
    lui $gp,%hi(_gp)
    addiu $gp,$gp,%lo(_gp)
    jal exercise
    nop
    move $a0,$v0
    li $v0,4001
    syscall
