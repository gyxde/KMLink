package io.kmlink.android;

import java.util.function.Predicate;
import java.util.function.Supplier;

/** Activity bindings can lag behind the live service during an installer round trip. */
final class UpdateSessionGuard {
    static <T> T active(T bound, Supplier<T> current) {
        T live = current.get();
        return live != null ? live : bound;
    }

    static <T> boolean allowsUpdate(T bound, Supplier<T> current, Predicate<T> busy) {
        T live = current.get();
        return (bound == null || !busy.test(bound))
                && (live == null || live == bound || !busy.test(live));
    }
}
