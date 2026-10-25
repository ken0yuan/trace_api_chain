"""Independent offline SQL replay, query and HTML commands."""
import argparse
import json

def main():
    parser=argparse.ArgumentParser(description='Offline SQL graph refresh and API export; never runs tests')
    sub=parser.add_subparsers(dest='command',required=True)
    replay=sub.add_parser('replay',help='Refresh one shared SQL database from raw captures; does not render HTML')
    replay.add_argument('captures',nargs='+');replay.add_argument('--db',required=True)
    replay.add_argument('--mode',choices=('full','objects'))
    replay.add_argument('--source-root',action='append',default=[],metavar='LABEL=PATH')
    replay.add_argument('--slot',help='Explicit latest-evidence slot, only for a single input')
    listing=sub.add_parser('apis',help='List observed APIs in the database');listing.add_argument('--db',required=True)
    query=sub.add_parser('query',help='Read an API graph from SQL');query.add_argument('--db',required=True);query.add_argument('--api',required=True);query.add_argument('--out')
    render=sub.add_parser('html',help='Generate C03 solely from SQL');render.add_argument('--db',required=True);render.add_argument('--api',required=True);render.add_argument('--out',required=True)
    args=parser.parse_args()
    from .database import refresh,list_apis,query as query_api,render_api
    from .native import save
    from pathlib import Path
    if args.command=='replay':
        from .pipeline import prepare
        if args.slot and len(args.captures)!=1:parser.error('--slot requires one capture')
        roots=dict(p.split('=',1) for p in args.source_root) or None
        results=[]
        for capture in args.captures:
            data=prepare(capture,args.mode)
            report=refresh(args.db,data,roots=roots,slot=args.slot)
            save(Path(capture)/'replay-result.json',report);results.append(report)
        print(json.dumps(results,ensure_ascii=False,indent=2));return 0
    if args.command=='apis':report=list_apis(args.db)
    elif args.command=='query':
        report=query_api(args.db,args.api)
        if args.out:save(Path(args.out),report)
    else:report=render_api(args.db,args.api,args.out)
    print(json.dumps(report,ensure_ascii=False,indent=2));return 0
