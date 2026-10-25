/* Offline ELF virtual-address -> declaration lookup. No runtime instrumentation. */
#include <elfutils/libdwfl.h>
#include <dwarf.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>

static void string(const char *s) {
  if (!s) { fputs("null", stdout); return; }
  putchar('"');
  for (const unsigned char *p=(const unsigned char *)s; *p; p++) {
    if (*p=='"' || *p=='\\') { putchar('\\'); putchar(*p); }
    else if (*p<32) printf("\\u%04x",*p);
    else putchar(*p);
  }
  putchar('"');
}
static const char *attr(Dwarf_Die *die, unsigned code) {
  Dwarf_Attribute a;
  return dwarf_attr_integrate(die,code,&a) ? dwarf_formstring(&a) : NULL;
}
int main(int argc,char **argv) {
  if (argc!=2) return 2;
  struct rlimit memory={8ULL*1024*1024*1024,8ULL*1024*1024*1024};
  struct rlimit cpu={180,180};
  if (setrlimit(RLIMIT_AS,&memory) || setrlimit(RLIMIT_CPU,&cpu)) return 3;
  Dwfl_Callbacks cb={.section_address=dwfl_offline_section_address};
  Dwfl *dwfl=dwfl_begin(&cb);
  Dwfl_Module *module=dwfl ? dwfl_report_offline(dwfl,argv[1],argv[1],-1) : NULL;
  if (!module || dwfl_report_end(dwfl,NULL,NULL)) return 4;
  Dwarf_Addr bias=0;
  Dwarf *dwarf=dwfl_module_getdwarf(module,&bias);
  unsigned long long pc;
  while (scanf("%llx",&pc)==1) {
    Dwarf_Addr lookup_bias=0;
    Dwarf_Die *cu=dwarf ? dwfl_module_addrdie(module,pc+bias,&lookup_bias) : NULL;
    Dwarf_Die *scopes=NULL, *function=NULL;
    int count=cu ? dwarf_getscopes(cu,pc+bias-lookup_bias,&scopes) : 0;
    int matches=0;
    for(int i=0;i<count;i++) if(dwarf_tag(&scopes[i])==DW_TAG_subprogram) {
      function=&scopes[i]; matches++;
    }
    printf("{\"offset\":%llu,\"function\":",pc);
    int line=0;
    if(matches==1 && dwarf_decl_line(function,&line)==0 && line>0) {
      Dwarf_Die owner;
      const char *dir=dwarf_diecu(function,&owner,NULL,NULL) ? attr(&owner,DW_AT_comp_dir) : NULL;
      fputs("{\"kind\":\"definition\",\"file\":",stdout); string(dwarf_decl_file(function));
      printf(",\"line\":%d,\"name\":",line); string(attr(function,DW_AT_name));
      fputs(",\"linkage_name\":",stdout); string(attr(function,DW_AT_linkage_name));
      fputs(",\"comp_dir\":",stdout); string(dir); putchar('}');
    } else fputs("null",stdout);
    fputs(",\"reader\":\"libdw\"}\n",stdout);
    free(scopes);
  }
  dwfl_end(dwfl);
  return 0;
}
