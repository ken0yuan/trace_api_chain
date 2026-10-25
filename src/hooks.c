#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

/* This validation backend explicitly scopes each recording to the calling
 * thread. No dladdr, symbol lookup, locks or write syscalls in the hot path.
 * A fixed budget and short writes are reported, never silently discarded. */
struct event { uint64_t time_ns, function, caller; uint32_t pid, tid, kind, reserved; };
static __thread struct event *events;
static __thread size_t used, capacity;
static __thread uint32_t sequence;
static __thread int enabled, fd = -1, failed;
static __thread uint32_t process_id, thread_id;

int trace_index_interposed(void) {
    Dl_info self = {0}, active = {0};
    dladdr((void *)trace_index_interposed, &self);
    dladdr(dlsym(RTLD_DEFAULT, "__cyg_profile_func_enter"), &active);
    return self.dli_fbase && self.dli_fbase == active.dli_fbase;
}

void trace_index_context(const char *test, const char *phase) { (void)test; (void)phase; }
void trace_index_python_location(const char *fn, const char *b, const char *p, const char *h, int l) {
    (void)fn; (void)b; (void)p; (void)h; (void)l;
}

int trace_index_configure(const char *path) {
    if (fd >= 0) return -1;
    const char *limit = getenv("TRACE_API_CHAIN_MAX_EVENTS");
    capacity = limit ? strtoull(limit, NULL, 10) : 262144;
    if (!capacity || capacity > 10000000) return -1;
    if (!events) events = malloc(capacity * sizeof(*events));
    if (!events) return -1;
    fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) return -1;
    used = 0; failed = 0;
    process_id = (uint32_t)getpid(); thread_id = (uint32_t)syscall(SYS_gettid);
    enabled = 1;
    return 0;
}

static void record(void *function, void *caller, uint32_t kind) {
    if (!enabled) return;
    if (used >= capacity) { failed = 1; return; }
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    events[used++] = (struct event){
        (uint64_t)now.tv_sec * 1000000000ULL + now.tv_nsec,
        (uintptr_t)function, (uintptr_t)caller, process_id, thread_id, kind, sequence++};
}
void __cyg_profile_func_enter(void *fn, void *caller) { record(fn, caller, 1); }
void __cyg_profile_func_exit(void *fn, void *caller) { record(fn, caller, 2); }

static int write_all(const char *data, size_t count) {
    while (count) {
        ssize_t n = write(fd, data, count);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        data += n; count -= (size_t)n;
    }
    return 0;
}
int trace_index_stop(void) {
    enabled = 0;
    if (fd < 0) return -1;
    if (write_all("CCADDR01", 8) || write_all((char *)events, used*sizeof(*events))) failed = 1;
    if (close(fd)) failed = 1;
    fd = -1;
    return failed ? -1 : 0;
}

size_t trace_index_event_count(void) { return used; }
