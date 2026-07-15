"""Phase 3 Step A: 生成数据底稿 + AI 拆解任务说明（按叙事框架组织的笔记结构层拆解）。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import common

VISUAL_STYLE_SPEC = """## HTML 报告视觉风格规范——"拆解简报"

严格按以下规范生成，不得使用灰褐底色+砖红强调色+无圆角无阴影无白卡的工业档案风，
也不得沿用暖米白底+高对比衬线+陶土色点缀这种常见 AI 生成设计默认风格：

- 背景色 `#FAFAF9`（近白）；强调色 `#5546FF`（靠紫，唯一强调色，克制使用）；
  强调色浅调 `#ECE9FF`（用于底色高亮/进度条填充）；正文色 `#16151A`（近黑）；
  次要文字 `#6E6B76`；分割线 `#E5E3E0`
- 标题与大数字字体 Google Fonts `Manrope`（几何无衬线，不用衬线体）；
  中文全部用 `Noto Sans SC`（黑体，按粗细分级，标题 700/正文 400）；
  英文正文与数据标签用 `Inter`
- 模块外观：大圆角 16px + 极轻柔阴影（不用描边，不用无阴影硬边框）的白色浮起卡片
- 首屏：这批笔记的核心数据用大字号数字排版呈现（Apple 式的巨大自信数字），不是"大数字+小标签+渐变"模板化写法
- 招牌元素：桌面端加一条侧边"拆解进度轨"——一级节点是归纳出来的几种叙事框架（比如痛点型/测评型/教程型），
  每个框架展开后是标题公式/开头模式/中间结构/结尾方式/CTA 类型五个二级节点，
  外加一个"代表作逐句拆解"节点；随滚动位置高亮推进，兼具导航和"逐层拆解"的视觉隐喻；
  移动端可退化为顶部进度条
- 动效（原生 JS，无外部库）：首屏一次性编排好的进场动画（数字滚动计数 + 标题浮现），
  滚动时侧边进度轨跟随高亮；克制，不叠加额外的散点特效（如卡片 hover 描边变色之类）
- 折叠面板用原生 `<details><summary>`；响应式断点 768px，移动端隐藏侧边进度轨
- 技术要求：单文件 HTML，手写 CSS，禁止引入 Tailwind CDN 等外部框架
"""

SKILL_FOLDER_SPEC = """## 写作指南 Skill 文件夹规范

产出路径：`{name}_写作指南.skill/SKILL.md`（必须是文件夹，不能是单个 `.skill.md` 文件）

SKILL.md 必须支持两种调用方式，在文档开头明确写出调用说明：

1. **1:1 模仿模式**（默认）：直接照搬本文档记录的对标公式与写法创作。
2. **融合风格模式**：用户在触发时指定"保留我自己的哪些维度（如语言习惯/结尾方式），其余维度套用对标笔记的公式"。
   AI 按用户指定的维度做混合，而不是全盘照搬。文档中需要列出可供用户选择混合的维度清单
   （至少涵盖：标题公式、开头模板、中间结构、结尾方式、CTA 策略、框架选择、语言习惯），
   每个维度给出对标笔记的对应结论，供用户挑选保留/替换。

内容章节按叙事框架分组组织，每个框架下给出可执行结论（不是原始统计数字，是给 AI 创作时直接可用的规则）：
- 这个框架的适用场景（什么样的产品/话题适合用这个框架）
- 标题公式（带示例）/ 开头模式 / 中间结构 / 结尾方式 / CTA 类型，各自的可执行写法规则
- 该框架下 1-2 篇代表作的逐句/逐段拆解

报告和 Skill 都只产出对标笔记的套路规律，不主动询问或写入用户自己的业务信息——
用户以后每次要写新笔记，自己在对话里说明业务，由这份 Skill 现场套用规律。
"""


def build_data_digest(name: str, profile: dict, analysis: dict) -> str:
    lines = [f"# {name} 数据底稿\n"]

    if profile:
        lines.append("## 账号基础信息（仅作身份标识，不参与分析）")
        lines.append(f"- 昵称：{profile.get('nickname', '')}")
        lines.append(f"- 简介：{profile.get('desc', '')}\n")

    lines.append("## 全量统计")
    lines.append(f"- 分析笔记总数：{analysis['total']}")
    lines.append(f"- 均赞：{analysis['avg_liked']} / 均藏：{analysis['avg_collected']} / 均评：{analysis['avg_comment']}")
    lines.append(f"- 藏赞比：{analysis['collect_like_ratio']}")
    lines.append(f"- 图文 vs 视频：{analysis['image_video_ratio']}")
    lines.append(f"- 标题公式分布：{analysis['title_formula_distribution']}")
    lines.append(f"- 高频标签 TOP20：{analysis['tag_frequency']}\n")

    lines.append("## 全部笔记全文（按点赞数从高到低排列，供逐篇归类框架与拆解结构）")
    for i, note in enumerate(analysis["all_notes"], start=1):
        lines.append(
            f"{i}. 《{note.get('title', '')}》 赞{note.get('liked_count', 0)} "
            f"藏{note.get('collected_count', 0)} 评{note.get('comment_count', 0)}"
        )
        desc = note.get("desc", "")
        if desc:
            lines.append(f"   正文：{desc}")
        tags = note.get("tags", [])
        if tags:
            lines.append(f"   标签：{', '.join(tags)}")
    return "\n".join(lines)


def build_ai_task(name: str) -> str:
    lines = [f"# {name} AI 拆解任务\n"]
    lines.append(
        "请基于同目录下的 `{}_数据底稿.md`，按以下流程产出拆解结论，"
        "再生成两个最终产出物。每完成一个立即写入磁盘，不等另一个完成。\n".format(name)
    )

    lines.append("## 第一步：单篇标注")
    lines.append(
        "逐篇阅读“全部笔记全文”里的每一篇，标注 6 个维度：标题公式、开头模式、"
        "中间结构、结尾方式、CTA 类型、叙事框架。标题公式可参考数据底稿里的正则分类结果，"
        "其余 5 项由你阅读全文后判断。叙事框架不是写死的分类表，由你读完这批笔记后自己归纳"
        "（比如痛点型/测评型/教程型），命名要贴合实际内容\n"
    )

    lines.append("## 第二步：框架分组")
    lines.append("把标注完的笔记按叙事框架分组，同一框架下的笔记归到一起\n")

    lines.append("## 第三步：提炼规律")
    lines.append(
        "不用区分某个写法是因为博主个人习惯还是选题不同造成的——只要在这个框架分组里"
        "反复出现、值得抄的写法规律，就直接提炼成可执行规则。框架组内笔记数 ≥ 2 篇时做交叉提炼；"
        "只有 1 篇时直接把这篇的标注结果当结论用，跳过对比\n"
    )

    lines.append("## 第四步：代表作精读")
    lines.append("每个框架挑 1-2 篇高赞代表作，做逐句/逐段拆解，展示具体怎么写的，不是抽象规律\n")

    lines.append("## 第五步：产出可执行规则")
    lines.append(
        "结果导向：把第三步、第四步的结论合并成能直接套用的规则，不停留在“观察到的现象”描述。"
        "不需要询问或写入用户自己的业务信息，只产出对标笔记本身的套路规律\n"
    )

    lines.append(VISUAL_STYLE_SPEC)
    lines.append(SKILL_FOLDER_SPEC.format(name=name))

    lines.append("## 质量红线")
    lines.append(
        f"- HTML 报告文件名：`{name}_拆解报告.html`，报告正文必须包含"
        "「框架」「标题」「开头」「中间」「结尾」「CTA」这 6 个关键词锚点，且各部分均有实质内容"
    )
    lines.append(f"- Skill 文件夹路径：`{name}_写作指南.skill/SKILL.md`，必须是文件夹，不能是单个文件")
    lines.append("- 生成完毕后运行 `python scripts/utils/quality.py` 对应的校验逻辑（或等价手动检查）确认无遗漏")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="生成数据底稿 + AI 拆解任务说明")
    parser.add_argument("analysis_path", help="<id>_analysis.json 路径")
    parser.add_argument("name", help="产出物文件名标识：博主展示名（模式一）或批次标识（模式二）")
    parser.add_argument("-o", "--output", default="./output", help="输出目录，默认 ./output")
    parser.add_argument("--scan", default=None, help="<user_id>_scan.json 路径（模式一用于读取 profile，模式二可不传）")
    args = parser.parse_args()

    analysis = common.load_json(args.analysis_path)
    profile = {}
    if args.scan:
        scan_data = common.load_json(args.scan)
        profile = scan_data.get("profile", {})

    digest = build_data_digest(args.name, profile, analysis)
    task = build_ai_task(args.name)

    out_dir = Path(args.output)
    digest_path = out_dir / f"{args.name}_数据底稿.md"
    task_path = out_dir / f"{args.name}_AI拆解任务.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(digest, encoding="utf-8")
    task_path.write_text(task, encoding="utf-8")

    print(f"✅ 数据底稿：{digest_path}")
    print(f"✅ AI拆解任务：{task_path}")


if __name__ == "__main__":
    main()
