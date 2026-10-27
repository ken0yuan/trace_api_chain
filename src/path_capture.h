/* Native path storage. Enter/exit stacks are checked while recording; repeated
 * paths are counted, not expanded into per-invocation timestamp records.
 * Window support is separate so merging cannot invent a branch in a window. */
#include <string.h>
#define PATH_DEPTH 8192
struct path_node { uint64_t function, caller; uint32_t parent, reserved; };
struct path_support { uint64_t window, enters, exits; uint32_t node, reserved; };
struct path_frame { uint64_t window, function, caller; uint32_t node, support; };
static __thread struct path_node *path_nodes;
static __thread struct path_support *path_supports;
static __thread uint32_t *path_node_hash, *path_support_hash;
/* Keep preload TLS small: CANN creates threads with small explicit stacks.
 * Allocate recording buffers only on the thread that starts collection. */
static __thread struct path_frame *path_stack;
static __thread uint32_t path_node_used, path_support_used, path_depth;
static __thread size_t path_node_cap, path_support_cap, path_node_slots, path_support_slots;
static __thread uint64_t path_window, path_events, path_exit_callsite_variations;
static __thread int path_failed, path_initialized;

static uint64_t path_mix(uint64_t x) {
    x ^= x >> 30; x *= UINT64_C(0xbf58476d1ce4e5b9);
    x ^= x >> 27; x *= UINT64_C(0x94d049bb133111eb);
    return x ^ (x >> 31);
}
static size_t path_slots(size_t n) { size_t s=1; while(s<n*2) s<<=1; return s; }
static int path_initialize(void) {
    if(path_initialized) return path_failed ? -1 : 0;
    path_initialized=1;
    const char *n=getenv("TRACE_API_CHAIN_MAX_PATHS"), *s=getenv("TRACE_API_CHAIN_MAX_EVENTS");
    path_node_cap=n ? strtoull(n,NULL,10) : 262144;
    path_support_cap=s ? strtoull(s,NULL,10) : 262144;
    if(!path_node_cap || path_node_cap>10000000 || !path_support_cap || path_support_cap>10000000) { path_failed|=1; return -1; }
    path_node_slots=path_slots(path_node_cap);path_support_slots=path_slots(path_support_cap);
    path_nodes=calloc(path_node_cap,sizeof(*path_nodes));
    path_stack=calloc(PATH_DEPTH,sizeof(*path_stack));
    path_supports=calloc(path_support_cap,sizeof(*path_supports));
    path_node_hash=calloc(path_node_slots,sizeof(*path_node_hash));
    path_support_hash=calloc(path_support_slots,sizeof(*path_support_hash));
    if(!path_stack || !path_nodes || !path_supports || !path_node_hash || !path_support_hash) { path_failed|=1; return -1; }
    process_id=(uint32_t)getpid();thread_id=(uint32_t)syscall(SYS_gettid);
    return 0;
}
int trace_paths_begin(uint64_t window) {
    if(path_initialize()) return -1;
    path_window=window;path_active=1;return 0;
}
int trace_paths_pause(void) { path_active=0;return path_failed ? -1 : 0; }
uint64_t trace_paths_event_count(void) { return path_events; }
uint64_t trace_paths_exit_callsite_variations(void) { return path_exit_callsite_variations; }
static void path_record_enter(void *fn,void *caller) {
    if(path_failed) return;
    if(path_depth>=PATH_DEPTH) { path_failed|=2;return; }
    uint64_t f=(uintptr_t)fn,c=(uintptr_t)caller;
    uint32_t parent=(path_depth && path_stack[path_depth-1].window==path_window) ? path_stack[path_depth-1].node : 0;
    size_t slot=path_mix(f ^ path_mix(c) ^ path_mix(parent)) & (path_node_slots-1);
    uint32_t id;
    for(;;slot=(slot+1)&(path_node_slots-1)) {
        id=path_node_hash[slot];
        if(!id) {
            if(path_node_used>=path_node_cap) { path_failed|=4;return; }
            id=++path_node_used;path_node_hash[slot]=id;
            path_nodes[id-1]=(struct path_node){f,c,parent,0};break;
        }
        struct path_node *p=&path_nodes[id-1];
        if(p->function==f && p->caller==c && p->parent==parent) break;
    }
    slot=path_mix(path_window ^ path_mix(id)) & (path_support_slots-1);
    uint32_t support;
    for(;;slot=(slot+1)&(path_support_slots-1)) {
        support=path_support_hash[slot];
        if(!support) {
            if(path_support_used>=path_support_cap) { path_failed|=8;return; }
            support=++path_support_used;path_support_hash[slot]=support;
            path_supports[support-1]=(struct path_support){path_window,0,0,id,0};break;
        }
        struct path_support *p=&path_supports[support-1];
        if(p->window==path_window && p->node==id) break;
    }
    path_supports[support-1].enters++;path_events++;
    path_stack[path_depth++]=(struct path_frame){path_window,f,c,id,support};
}
static void path_record_exit(void *fn,void *caller) {
    if(path_failed) return;
    if(!path_depth) { path_failed|=16;return; }
    struct path_frame *p=&path_stack[path_depth-1];
    /* GCC may inline the entry but outline the exit into a .part/.isra clone.
     * The same conceptual function then reports different call-site addresses.
     * Pair only the top function in its original window; never search/unwind
     * the stack to make an exit fit. Keep the entry call site as path metadata. */
    if(p->function!=(uintptr_t)fn || p->window!=path_window) { path_failed|=32;return; }
    if(p->caller!=(uintptr_t)caller) path_exit_callsite_variations++;
    path_supports[p->support-1].exits++;path_events++;path_depth--;
}
int trace_paths_finish(const char *path) {
    path_active=0;
    if(path_depth) path_failed|=64;
    struct { char magic[8]; uint64_t nodes,supports,events; uint32_t pid,tid,failed,reserved; } header={
        {'C','C','P','A','T','H','0','1'},path_node_used,path_support_used,path_events,process_id,thread_id,path_failed,0};
    _Static_assert(sizeof(header)==48,"path header layout");
    _Static_assert(sizeof(struct path_node)==24,"path node layout");
    _Static_assert(sizeof(struct path_support)==32,"path support layout");
    fd=open(path,O_WRONLY|O_CREAT|O_EXCL,0600);
    if(fd<0) return -1;
    int failed=write_all((const char *)&header,sizeof(header)) ||
        write_all((const char *)path_nodes,path_node_used*sizeof(*path_nodes)) ||
        write_all((const char *)path_supports,path_support_used*sizeof(*path_supports));
    if(close(fd)) failed=1;fd=-1;
    return (failed || path_failed) ? -1 : 0;
}
