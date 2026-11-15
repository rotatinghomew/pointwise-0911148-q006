TITLE = '应收回款预测台'
OPERATION = 'open_balance'
FORECAST = 'collection_forecast'

import argparse
import json

from app.httpd import create_server
from app.service import Service


def build_parser():
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument('--source', default='data/source.json')
    parser.add_argument('--database', default='var/analyses.sqlite3')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('inspect', help='保存业务覆盖分析')
    run.add_argument('--as-of', default='2026-09-30T23:59:59Z')
    run.add_argument('--request-id', default='coverage-september')
    forecast = commands.add_parser('forecast', help='保存八周回款分布预测')
    forecast.add_argument('--as-of', default='2026-09-30T23:59:59Z')
    forecast.add_argument('--request-id', default='forecast-september')
    forecast.add_argument('--weeks', type=int, default=8)
    forecast.add_argument('--paths', type=int, default=2000)
    forecast.add_argument('--options', default=None,
                          help='完整 options 的 JSON 字符串，含情景试算；提供后覆盖 weeks/paths')
    server = commands.add_parser('serve', help='启动本地分析接口')
    server.add_argument('--host', default='127.0.0.1')
    server.add_argument('--port', type=int, default=8080)
    commands.add_parser('demo', help='展示覆盖核对与回款预测')
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    service = Service(args.source, args.database)
    if args.command == 'serve':
        instance = create_server(service, args.host, args.port)
        print(json.dumps({'地址': instance.server_address}, ensure_ascii=False), flush=True)
        try:
            instance.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            instance.server_close()
    elif args.command == 'forecast':
        if args.options:
            options = json.loads(args.options)
        else:
            options = {'weeks': args.weeks, 'paths': args.paths}
        request = {'operation': FORECAST, 'request_id': args.request_id,
                   'as_of': args.as_of, 'options': options}
        print(json.dumps(service.run(request), ensure_ascii=False, indent=2))
    else:
        as_of = getattr(args, 'as_of', '2026-09-30T23:59:59Z')
        request_id = getattr(args, 'request_id', 'demo-current')
        if args.command == 'demo':
            as_of = '2026-09-30T23:59:59Z'
            request_id = 'demo-current'
            coverage = service.run({'operation': OPERATION, 'request_id': 'demo-coverage',
                                    'as_of': as_of})
            forecast = service.run({
                'operation': FORECAST, 'request_id': 'demo-forecast', 'as_of': as_of,
                'options': {'weeks': 8, 'paths': 2000, 'scenarios': [
                    {'name': '重点客户延迟两周',
                     'clients': {'c3': {'delay_days': 14}}},
                    {'name': '重点客户承诺落空',
                     'clients': {'c8': {'add_nonpay': 0.30}}}]}})
            print(json.dumps({'open_balance': coverage,
                              'collection_forecast': forecast},
                             ensure_ascii=False, indent=2))
            return
        request = {'operation': OPERATION, 'request_id': request_id, 'as_of': as_of}
        print(json.dumps(service.run(request), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
