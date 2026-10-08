/* Rage Wars us-rev1 RSP task loader (ROM DD840-DD910).
 * Semantic reference: n64decomp/sm64 rsp/rspboot.s and rsp/rsp_defs.inc,
 * distributed under CC0-1.0. GNU-as adaptation checked against owner ROM.
 * Execution IMEM: 04001000; host-resident storage: 800DCC40.
 */
.set noreorder
.set noat
.section .rsp.boot,"ax",@progbits
.balign 16
.equ OSTASK_DMEM, 0x0fc0
.equ TASK_FLAGS, 0x04
.equ TASK_UCODE, 0x10
.equ TASK_DATA, 0x18
.equ TASK_DATA_SIZE, 0x1c
.equ TASK_DP_WAIT, 0x0002
.equ SP_STATUS_YIELD, 0x0080
.equ DPC_DMA_BUSY, 0x0100
.equ SP_FINISH_YIELD, 0x5200
.globl ragewars_rsp_boot
.type ragewars_rsp_boot,@function
ragewars_rsp_boot:
    j boot_check_dp
     addi $1, $0, OSTASK_DMEM
boot_load_ucode:
    lw $2, TASK_UCODE($1)
    addi $3, $0, 0x0f7f
    addi $7, $0, 0x1080
    mtc0 $7, $0 /* SP_MEM_ADDR */
    mtc0 $2, $1 /* SP_DRAM_ADDR */
    mtc0 $3, $2 /* SP_RD_LEN */
boot_wait_ucode_dma:
    mfc0 $4, $6 /* SP_DMA_BUSY */
    bne $4, $0, boot_wait_ucode_dma
     nop
    jal boot_check_yield
     nop
    jr $7
     mtc0 $0, $7 /* SP_SEMAPHORE */
boot_check_yield:
    mfc0 $8, $4 /* SP_STATUS */
    andi $8, $8, SP_STATUS_YIELD
    bne $8, $0, boot_yield
     nop
    jr $31
boot_yield:
     mtc0 $0, $7 /* also the return delay slot: release semaphore */
    ori $8, $0, SP_FINISH_YIELD
    mtc0 $8, $4
    break
    nop
boot_check_dp:
    lw $2, TASK_FLAGS($1)
    andi $2, $2, TASK_DP_WAIT
    beq $2, $0, boot_load_data
     nop
    jal boot_check_yield
     nop
    mfc0 $2, $11 /* DPC_STATUS */
    andi $2, $2, DPC_DMA_BUSY
    bgtz $2, boot_check_yield
     nop
boot_load_data:
    lw $2, TASK_DATA($1)
    lw $3, TASK_DATA_SIZE($1)
    addi $3, $3, -1
boot_wait_dma_space:
    mfc0 $30, $5 /* SP_DMA_FULL */
    bne $30, $0, boot_wait_dma_space
     nop
    mtc0 $0, $0 /* load data at DMEM zero */
    mtc0 $2, $1
    mtc0 $3, $2
boot_wait_data_dma:
    mfc0 $4, $6
    bne $4, $0, boot_wait_data_dma
     nop
    jal boot_check_yield
     nop
    j boot_load_ucode
     nop
    nop /* resident resource's final instruction alignment */
.size ragewars_rsp_boot, .-ragewars_rsp_boot
