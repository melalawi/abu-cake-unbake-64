RW-A public stdarg SDK declaration parsing route.
include/stdarg.h is the complete byte-exact generated public provider, retained
with both GCC/IDO branches and their definitions. The common/unused.h context
is the original va_list typedef line. This fixture does not replace a provider.
The factory native compile already accepts the provider; syntax checks here
use a 32-bit host C frontend on its GCC branch to verify strict/GNU modes.
