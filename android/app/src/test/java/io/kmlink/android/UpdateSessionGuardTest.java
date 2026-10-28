package io.kmlink.android;

import java.util.concurrent.atomic.AtomicInteger;
import org.junit.Test;
import static org.junit.Assert.*;

public final class UpdateSessionGuardTest {
    private static final class Session { final boolean busy; Session(boolean busy){this.busy=busy;} }
    @Test public void missingActivityBindingStillChecksLiveBusyService() {
        Session live=new Session(true);AtomicInteger lookups=new AtomicInteger();
        assertFalse(UpdateSessionGuard.allowsUpdate(null,()->{lookups.incrementAndGet();return live;},s->s.busy));
        assertEquals(1,lookups.get());
        assertSame(live,UpdateSessionGuard.active(null,()->live));
    }
    @Test public void aNewLiveServiceCannotBeHiddenByAStaleIdleBinding() {
        Session stale=new Session(false),live=new Session(true);
        assertFalse(UpdateSessionGuard.allowsUpdate(stale,()->live,s->s.busy));
        assertSame(live,UpdateSessionGuard.active(stale,()->live));
    }
    @Test public void onlyAnIdleOrAbsentServicePermitsLocalUpdate() {
        Session idle=new Session(false),busy=new Session(true);
        assertTrue(UpdateSessionGuard.<Session>allowsUpdate(null,()->null,s->s.busy));
        assertTrue(UpdateSessionGuard.allowsUpdate(idle,()->idle,s->s.busy));
        assertFalse(UpdateSessionGuard.allowsUpdate(busy,()->null,s->s.busy));
        assertSame(idle,UpdateSessionGuard.active(idle,()->null));
    }
}
