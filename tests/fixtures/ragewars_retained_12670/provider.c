#include "common/types_1dc8418c21db.h"
#include "span_1000/code_8020AF9C.h"
#include "shared/route_relation_grid.h"

s32 func_8020D1CC_de(void *arg0, s32 arg1, s32 arg2) {
    ObjectLinks14 *table = arg0;
    RouteRelationBytes *bytes = (RouteRelationBytes *)table->unk_10;
    s32 index = (arg1 * table->unk_4 + arg2) * bytes->stride;
    return *(index + bytes->entries);
}
