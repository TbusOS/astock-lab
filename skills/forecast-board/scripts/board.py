#!/usr/bin/env python3
"""board —— 预测看板的命令入口。

它是什么:前面各模块(取数、统计底线、系统算法、两个 AI、存档、出题、结算、原因、重跑、页面)
各管一段,这里把它们串成几条命令。没有它,出一次预测要手动按顺序调十几个函数,
漏一步(比如忘了写决策日志、忘了出题)就没人发现。

    board.py inputs --check [代码…]     只取数、列数据清单和缺什么,不出预测(平时分析也先跑这条)
    board.py update                     刷新行情、事件、持仓,用最新一次存档重画页面,不改预测
    board.py forecast [--no-codex] [--notes 查证笔记.md]
                                        取数 → 统计底线 → 系统算法 → 两个 AI → 存档 → 出题 → 决策日志 → 页面
    board.py resolve                    结算到期的题,追加到 outcomes.jsonl;错题给原因初值
    board.py check [--notify]           每日检查:收盘价、卖出线、改主意条件、到期题 → status.json(+ 系统通知)
    board.py replay --run <存档> --code <提交号|HEAD|run> [--corrected 文件]
                                        用指定版本的代码重算那次存档的两层,和原来比,记进 replays.jsonl
    board.py recompute --inputs <文件> --seed <n>
                                        给 replay 用:只读那个输入包,stdout 只输出两层的 JSON
    board.py add-question …             聊天里给的判断写成题(碰价 / 某天高于 / 到某个倍数)
    board.py confirm --qid … --probs data_gap=60,system=30,luck=10 --note …
                                        人确认一道错题的原因概率

配置和存档在内部仓:先 export STOCK_LAB=<内部仓>。各子命令的细节见 board_forecast.py、board_settle.py、
board_layers.py;检查是 scripts/check_board_cli.py。
退出码:0 做完了;1 做了但有要处理的(取不到数据、题号重复、有题结算不了……);2 用法不对、或盘中不出预测。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # 从 tools/board.py 软链跑也找得到旁边的模块

import fb_common  # noqa: E402,F401  必须最先:它拍下带代理的环境,之后 prices 会加载 market.py 删掉代理变量


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="board.py", description="预测看板:出预测、结算、每日检查、重跑。",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="子命令")

    s = sub.add_parser("inputs", help="只取数,列数据清单和缺什么")
    s.add_argument("--check", nargs="*", required=True, metavar="代码", help="要查的股票,不写 = 配置里全部")

    sub.add_parser("update", help="刷新行情、事件、持仓,重画页面(不改预测)")

    s = sub.add_parser("forecast", help="出一次预测并存档")
    s.add_argument("--no-codex", action="store_true", help="这次只让 Claude 答")
    s.add_argument("--notes", metavar="文件", help="查证笔记(markdown),放进输入包,只给 AI 参考")
    s.add_argument("--seed", type=int, help="随机种子(默认用 as_of 的日期数字,配置里写了 seed 就用它)")

    sub.add_parser("resolve", help="结算到期的题")

    s = sub.add_parser("check", help="每日检查")
    s.add_argument("--notify", action=argparse.BooleanOptionalAction, default=False,
                   help="有触发就弹系统通知(定时任务用 --notify)")

    s = sub.add_parser("replay", help="用指定版本的代码重算一次存档的两层")
    s.add_argument("--run", required=True, metavar="存档", help="「日期/时分秒」或完整路径")
    s.add_argument("--code", required=True, metavar="版本", help="提交号 / 分支 / HEAD;写 run = 存档当时那份代码")
    s.add_argument("--corrected", metavar="文件", help="修正后的输入(只能改数据,每项要证明当时已公开)")

    s = sub.add_parser("recompute", help="(给 replay 用)只读输入包,输出两层 JSON")
    s.add_argument("--inputs", required=True, metavar="文件")
    s.add_argument("--seed", required=True, type=int)

    s = sub.add_parser("add-question", help="聊天里给的判断写成一道题")
    s.add_argument("--code", required=True)
    s.add_argument("--type", required=True, choices=("touch", "above_on", "multiple_by"))
    s.add_argument("--ratio", type=float, help="touch / above_on:价位 ÷ 开题日收盘")
    s.add_argument("--k", type=float, help="multiple_by:倍数")
    win = s.add_mutually_exclusive_group()
    win.add_argument("--n", type=int, help="touch / multiple_by:往后几个交易日内")
    win.add_argument("--end", help="touch / multiple_by:截止日期 YYYY-MM-DD")
    s.add_argument("--day", help="above_on:看哪一天的收盘 YYYY-MM-DD")
    s.add_argument("--dir", choices=("up", "down"), help="touch:往上碰还是往下碰(不写按比值定)")
    s.add_argument("--opened", help="开题日 YYYY-MM-DD(不写 = 最后一个收盘那天)")
    s.add_argument("--prob", required=True, type=int, help="会发生的概率,0~100 的整数")
    s.add_argument("--author", required=True, choices=("claude", "codex"))
    s.add_argument("--note", required=True, help="这道题从哪来、怎么换算的")

    s = sub.add_parser("confirm", help="确认一道错题的原因概率")
    s.add_argument("--qid", required=True)
    s.add_argument("--probs", required=True, help="原因=整数,逗号隔开,加起来 100;没写的原因算 0")
    s.add_argument("--note", required=True)
    s.add_argument("--who", default="user",
                   help="确认人(默认 user,不分大小写;写别的名字照样记下、会提示,成绩页只算提议,不算人确认)")
    s.add_argument("--evidence", action="append", default=[], help="证据,一条一个 --evidence,可写多条")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "recompute":                 # 只加载算两层要的模块,stdout 只有那段 JSON
        import board_layers
        return board_layers.cmd_recompute(args)
    if args.cmd in ("inputs", "update", "forecast"):
        import board_forecast
        return {"inputs": board_forecast.cmd_inputs, "update": board_forecast.cmd_update,
                "forecast": board_forecast.cmd_forecast}[args.cmd](args)
    import board_settle
    return {"resolve": board_settle.cmd_resolve, "check": board_settle.cmd_check,
            "add-question": board_settle.cmd_add_question, "confirm": board_settle.cmd_confirm,
            "replay": board_settle.cmd_replay}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
