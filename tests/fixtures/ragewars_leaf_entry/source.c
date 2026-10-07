#include "span_1000/code_802BB67C.h"

ThreadNode *func_802BC558_de(ThreadNode **queue) {
    ThreadNode *node;
    ThreadNode *next;

    node = *queue;
    next = node->next;
    node->next = 0;
    *queue = next;
    return node;
}
