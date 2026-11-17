"""文化档案检索夹具：核实的公开正文与按业务状态编写的纯合成案例。

来源、字形整理规则及逐条检索预期见 docs/rag-data.md；不导入真实档案。
夹具只提供数据，可信记录仍由 catalog._entry 统一构造。
"""
from __future__ import annotations

from dataclasses import dataclass

from common.schemas import SourceType, Zone


@dataclass(frozen=True)
class CorpusFixture:
    source_id: str
    title: str
    locator: str
    text: str
    quote: str
    source_type: SourceType
    zone: Zone
    keywords: tuple[str, ...]
    expected_query: str
    expected_sources: tuple[str, ...]
    public_topics: tuple[str, ...] = ()


def _classic(source_id: str, title: str, locator: str, quote: str,
             keywords: tuple[str, ...], query: str) -> CorpusFixture:
    return CorpusFixture(source_id, title, locator, f"《{title}·{locator}》：{quote}。",
                         quote, SourceType.PUBLIC_CLASSIC, Zone.GREEN, keywords,
                         query, (source_id,), keywords)


def _public(source_id: str, locator: str, quote: str, source_type: SourceType,
            keywords: tuple[str, ...], query: str) -> CorpusFixture:
    kind = "模拟公开文化 FAQ" if source_type == SourceType.FAQ else "模拟公开编辑规范"
    title = f"{kind}（非典籍原文）"
    return CorpusFixture(source_id, title, locator, f"{title}·{locator}：{quote}。",
                         quote, source_type, Zone.GREEN, keywords,
                         query, (source_id,), keywords)


def _red(source_id: str, locator: str, quote: str, context: str,
         keywords: tuple[str, ...], query: str) -> CorpusFixture:
    text = f"【纯合成演示；无真实用户秘密或个人信息】{locator}：{quote}。{context}"
    return CorpusFixture(source_id, "纯合成文化档案红区演示样例", locator, text, quote,
                         SourceType.ORIGINAL, Zone.RED, keywords, query, (source_id,))


# 《素问》据四库全书本卷一核实；只加标点、简化字，不把现代讲解混入原文。
# 底本作“恬惔”；“恬淡虚无”仅作为旧查询的检索别名保留。
FIXTURES = (
    _classic("suwen-1", "黄帝内经·素问", "上古天真论",
             "法于阴阳，和于术数",
             ("法于阴阳", "和于术数", "上古天真论", "素问", "黄帝内经"), "法于阴阳"),
    _classic("suwen-2", "黄帝内经·素问", "四气调神大论",
             "春三月，此谓发陈，天地俱生，万物以荣",
             ("四时养生", "四气调神", "四季", "春三月", "素问", "黄帝内经"), "四时养生"),
    _classic("suwen-3", "黄帝内经·素问", "上古天真论",
             "恬惔虚无，真气从之，精神内守，病安从来",
             ("恬淡虚无", "恬惔虚无", "真气从之", "精神内守", "上古天真论", "素问", "黄帝内经"),
             "恬淡虚无"),
    _public("faq-0", "用途与免责", "本平台仅作文化辅助参考，不替代执业医师诊疗",
            SourceType.FAQ, ("faq", "文化辅助", "免责", "平台用途", "文化参考"), "平台用途"),
    _red("synthetic-red-1", "虚构流程甲", "红区材料仅在本地处理，发布须独立人工复核", "",
         ("合成红区", "红区演示", "虚构流程甲", "独立人工复核", "本地处理"), "虚构流程甲"),
    _red("synthetic-red-2", "虚构流程乙", "客户端批准标志不能授权红区转绿区", "",
         ("合成红区", "红区演示", "虚构流程乙", "客户端批准", "红区转绿区"), "虚构流程乙"),

    _classic("suwen-food", "黄帝内经·素问", "上古天真论",
             "食饮有节", ("食饮有节", "饮食节度"), "食饮有节"),
    _classic("suwen-routine", "黄帝内经·素问", "上古天真论",
             "起居有常，不妄作劳", ("起居有常", "不妄作劳", "起居作息"), "起居有常"),
    _classic("suwen-summer", "黄帝内经·素问", "四气调神大论",
             "夏三月，此谓蕃秀，天地气交，万物华实",
             ("夏三月", "此谓蕃秀", "夏季时序"), "夏三月"),
    _classic("suwen-autumn", "黄帝内经·素问", "四气调神大论",
             "秋三月，此谓容平，天气以急，地气以明",
             ("秋三月", "此谓容平", "秋季时序"), "秋三月"),
    _classic("suwen-winter", "黄帝内经·素问", "四气调神大论",
             "冬三月，此谓闭藏，水冰地坼，无扰乎阳",
             ("冬三月", "此谓闭藏", "冬季时序"), "冬三月"),
    _classic("suwen-seasonal", "黄帝内经·素问", "四气调神大论",
             "春夏养阳，秋冬养阴", ("春夏养阳", "秋冬养阴", "四时阴阳"), "春夏养阳"),

    # 《礼记·学记》正文；不将本任务自拟的编目与教学说明归入引文。
    _classic("xueji-jade", "礼记", "学记",
             "玉不琢，不成器。人不学，不知道",
             ("玉不琢", "人不学", "学记玉器", "礼记", "学记"), "玉不琢"),
    _classic("xueji-learning", "礼记", "学记",
             "虽有嘉肴，弗食，不知其旨也。虽有至道，弗学，不知其善也",
             ("虽有嘉肴", "虽有至道", "嘉肴至道", "礼记", "学记"), "虽有嘉肴"),
    _classic("xueji-reflection", "礼记", "学记",
             "学然后知不足，教然后知困",
             ("学然后知不足", "教然后知困", "知不足知困", "礼记", "学记"), "学然后知不足"),
    _classic("xueji-mutual", "礼记", "学记",
             "教学相长", ("教学相长", "相长之义", "礼记", "学记"), "教学相长"),
    _classic("xueji-friends", "礼记", "学记",
             "独学而无友，则孤陋而寡闻",
             ("独学而无友", "孤陋而寡闻", "同伴学习", "礼记", "学记"), "独学而无友"),

    # 公开说明只发布模拟规则，不包含待核授权书、未审批注或档案底稿。
    _public("faq-collection", "采集说明",
            "模拟文化档案采集先说明文化主题、用途与展示渠道，再核对授权凭据；演示不采集身份资料",
            SourceType.FAQ, ("采集说明", "文化档案采集"), "采集说明"),
    _public("faq-rights", "资料授权范围",
            "模拟授权按摘录、展陈、再分发分别列明范围；持有资料不等于获准公开或转授",
            SourceType.FAQ, ("资料授权范围", "摘录展陈", "再分发范围"), "资料授权范围"),
    _public("faq-withdrawal", "撤回说明",
            "模拟撤回申请先暂停相关摘录展示，再核验申请依据、通知使用方并记录处置；历史引用保留撤回提示",
            SourceType.FAQ, ("撤回说明", "历史引用提示"), "撤回说明"),
    _public("edit-source", "原文标注规范",
            "模拟编目将典籍正文、编者说明与现代讲解分栏；摘录标明书名篇章，字形整理单列说明",
            SourceType.APPROVED, ("原文标注规范", "正文讲解分栏", "字形整理"), "原文标注规范"),
    _public("edit-collation", "勘校标注规范",
            "模拟勘校并列底本与参校本字形，标明定位与取舍理由；未完成核验不得把编辑建议写成原文",
            SourceType.APPROVED, ("勘校标注规范", "底本参校", "取舍理由"), "勘校标注规范"),
    _public("edit-release", "发布复核规范",
            "模拟发布须分别复核引文定位、授权范围和展示内容，任一项待核即暂停发布；检索命中不表示获准发布",
            SourceType.APPROVED, ("发布复核规范", "引文定位复核", "展示内容复核"), "发布复核规范"),
    _public("edit-version", "版本标识规范",
            "模拟摘录绑定文本摘要与版本标识；版本冲突先暂停沿用旧引文，完成差异复核后生成新摘录",
            SourceType.APPROVED, ("版本标识规范", "文本摘要", "旧引文复核"), "版本标识规范"),

    # 以下状态与处置为文化档案业务形状的纯合成演示，并非实际授权或审核结果。
    _red("synthetic-red-collection", "采集授权待核",
         "拟采集的节俗讲述仅获展陈摘录许可，转写与再分发范围待核，底稿留在本地",
         "样例对象为虚构文化片段甲；输入为模拟范围表，输出为待核清单，补齐凭据后仍须独立复核。",
         ("采集授权待核", "节俗讲述", "转写范围待核"), "采集授权待核"),
    _red("synthetic-red-notes", "未审编辑批注",
         "批注建议把编者解释改为正文，引文复核尚未完成，该批注不得进入公开摘录",
         "样例对象为虚构稿本乙；批注状态为待审，输出保留建议与正文分栏而不自动采纳。",
         ("未审编辑批注", "批注分栏", "编者解释改为正文"), "未审编辑批注"),
    _red("synthetic-red-collation", "勘校异文待定",
         "模拟底本作甲字，参校本作乙字，定位一致但取舍理由待补，不得覆盖已发布正文",
         "两种字形仅为占位符，不冒充典籍异文；复核输入为并列记录，输出为待定勘校单。",
         ("勘校异文待定", "异文取舍待补", "参校本作乙字"), "勘校异文待定"),
    _red("synthetic-red-scope", "展陈许可边界",
         "模拟授权只允许展陈摘录，不允许底稿下载或转授；导出请求超出范围，维持本地受限状态",
         "样例为虚构授权片段丙；权限矩阵中展陈可复核，下载和转授均不可放行。",
         ("展陈许可边界", "底稿下载", "转授限制"), "展陈许可边界"),
    _red("synthetic-red-revocation", "授权撤回待核",
         "模拟撤回凭据尚待核验，先暂停引用并建立使用方通知清单，不以客户端批准继续展示",
         "样例对象为虚构摘录丁；处置记录保留待核与暂停状态，不包含任何联系人。",
         ("授权撤回待核", "暂停引用", "撤回凭据"), "授权撤回待核"),
    _red("synthetic-red-version", "底稿版本冲突",
         "模拟初稿与复核稿的引文定位不一致，旧摘录标为待复核，不按较新标签直接替换",
         "样例为虚构目录戊；输入为两份定位表，输出为差异清单与暂停发布记录。",
         ("底稿版本冲突", "引文定位不一致", "定位表差异"), "底稿版本冲突"),
    _red("synthetic-red-ocr", "转写疑字待校",
         "模拟扫描转写的疑字仅标为待校，不按自动识别结果修改引文；人工核对图文定位后再复核",
         "样例仅使用虚构字符甲乙，不附真实扫描图；输入为疑字清单，输出为保留原位的待校标记。",
         ("转写疑字待校", "疑字清单", "图文定位"), "转写疑字待校"),
    _red("synthetic-red-caption", "展签引用待审",
         "模拟展签把文化讲解误排在引号内，须分离讲解与引文并复核篇章，未审展签不得转为公开证据",
         "样例为虚构展签己；输出为版式修订建议，非真实典籍解释。",
         ("展签引用待审", "展签引号", "版式修订建议"), "展签引用待审"),
    _red("synthetic-red-review", "复核意见冲突",
         "模拟引文审核通过但授权审核待补，不能合并为通过；分别保留审核状态并暂停发布",
         "样例对象为虚构摘录庚；输入为两类模拟意见，输出为授权待补事项，不含审核人身份。",
         ("复核意见冲突", "授权审核待补", "分别保留审核状态"), "复核意见冲突"),
    _red("synthetic-red-derivative", "衍生摘录下架",
         "模拟底稿撤回后须沿引用关系定位展签与问答摘录，暂停衍生展示并记录处置，不删除核验依据",
         "样例为虚构引用链辛；输出为衍生项清单与处置状态，不能仅隐藏底稿而继续展示衍生内容。",
         ("衍生摘录下架", "衍生项清单", "虚构引用链辛"), "衍生摘录下架"),
)
