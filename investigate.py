"""Investigate actual behaviour of each target handler."""

import asyncio

import linling_tools_stdlib  # noqa: F401
from linling_core import Event, Scope, SqliteKVStore, TextSegment, User, registry
from linling_dsl import VM, parse


async def main() -> None:
    main_src = open("bot/rules/main.ling").read()
    script = parse(main_src, strict=False)

    def find_exact(trigger: str):
        for h in script.handlers:
            if h.trigger == trigger:
                return h
        return None

    async def run(
        handler,
        text: str,
        captures=None,
        seeds=None,
        sender: str = "12345",
        group: str = "67890",
    ) -> None:
        async with SqliteKVStore(bot_id="linling", db_path=":memory:") as kv:
            if seeds:
                for (s, f, k), v in seeds.items():
                    await kv.write(s, f, k, v)
            vm = VM(tool_registry=registry, kv=kv)
            event = Event(
                id="t",
                platform="test",
                bot_id="linling",
                scope=Scope(kind="group", id=group, platform="test"),
                sender=User(id=sender, platform="test", display_name="测试"),
                segments=[TextSegment(text=text)],
            )
            try:
                result = await vm.execute_handler(handler, event, captures=captures or [])
                texts = [s.text for s in result.segments if hasattr(s, "text")]
                imgs = [getattr(s, "url", None) for s in result.segments if not hasattr(s, "text")]
                print(f"  -> texts={texts}, images={imgs}")
            except Exception as e:
                print(f"  -> EXCEPTION {type(e).__name__}: {e}")

    cases = [
        (
            "1 查看昵称",
            find_exact("查看昵称(.*)"),
            "查看昵称12345",
            ["12345"],
            {("小苏苏/自定义昵称/昵称", "12345", "0"): "苏苏"},
        ),
        ("2 好运赠送", find_exact("好运赠送(.*)"), "好运赠送99999", ["99999"], None),
        (
            "3 查看消息",
            find_exact("(查看消息|消息)"),
            "查看消息",
            ["查看消息"],
            {("啊/主页系/最新消息", "12345", "0"): "test-message"},
        ),
        ("4 反馈吞玉", find_exact("反馈吞玉([0-9]+)"), "反馈吞玉55", ["55"], None),
        ("5 解码", find_exact("解码(.*)"), "解码%E4%BD%A0%E5%A5%BD", ["%E4%BD%A0%E5%A5%BD"], None),
        ("6 编码", find_exact("编码(.*)"), "编码你好", ["你好"], None),
        ("7 hex编码", find_exact("hex编码(.*)"), "hex编码hi", ["hi"], None),
        ("8 hex解码", find_exact("hex解码(.*)"), "hex解码6869", ["6869"], None),
        ("9 64解码", find_exact("64解码(.*)"), "64解码aGVsbG8=", ["aGVsbG8="], None),
        (
            "10 uni解码",
            find_exact("uni解码(.*)"),
            "uni解码\\u4f60\\u597d",
            ["\\u4f60\\u597d"],
            None,
        ),
        (
            "11 背包",
            find_exact("(.*)(背包|物品)(.*)"),
            "背包",
            ["", "背包", ""],
            {("啊/灵玉系/灵玉", "12345", "0"): "500", ("啊/禁言系/妖力", "12345", "0"): "200"},
        ),
        ("12 查看状态 nonadmin", find_exact("查看状态"), "查看状态", [], None),
        (
            "13 补偿",
            find_exact("补偿([0-9]+)数量([0-9]+)"),
            "补偿99999数量100",
            ["99999", "100"],
            None,
        ),
        (
            "14 补偿蛋壳",
            find_exact("补偿蛋壳([0-9]+)数量([0-9]+)"),
            "补偿蛋壳99999数量50",
            ["99999", "50"],
            None,
        ),
        (
            "15 送锦囊给",
            find_exact("送锦囊给@.*"),
            "送锦囊给@someone",
            [],
            {("啊/活动系/锦囊", "12345", "0"): "1"},
        ),
        (
            "16 兑换御妖符",
            find_exact("兑换御妖符([0-9]+)"),
            "兑换御妖符1",
            ["1"],
            {("啊/灵玉系/灵玉", "12345", "0"): "1000"},
        ),
        (
            "17 灵玉划转X给Y",
            find_exact("灵玉划转([0-9]+)给([0-9]+)"),
            "灵玉划转100给99999",
            ["100", "99999"],
            {("啊/灵玉系/灵玉", "12345", "0"): "1000"},
        ),
        (
            "18 我的灵玉",
            find_exact("我的灵玉"),
            "我的灵玉",
            [],
            {("啊/灵玉系/灵玉", "12345", "0"): "500"},
        ),
        (
            "19 打开锦囊",
            find_exact("打开锦囊"),
            "打开锦囊",
            [],
            {
                ("啊/活动系/锦囊", "12345", "0"): "1",
                ("啊/灵玉系/灵玉", "12345", "0"): "100",
                ("啊/67890/禁言卡", "12345", "0"): "0",
            },
        ),
        (
            "20 点亮花标",
            find_exact("点亮花标"),
            "点亮花标",
            [],
            {("啊/活动系/玫瑰花", "12345", "0"): "3"},
        ),
    ]

    for label, handler, text, caps, seeds in cases:
        print(f"\n[{label}] trigger={handler.trigger if handler else None!r}")
        if handler is None:
            print("  handler not found")
            continue
        await run(handler, text, captures=caps, seeds=seeds)


asyncio.run(main())
