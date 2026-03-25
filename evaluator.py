"""AI-powered candidate evaluation using Claude API."""

import json
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, EVAL_MODEL, DEFAULT_JD


class CandidateEvaluator:
    """Evaluates candidates against job requirements using Claude."""

    def __init__(self):
        self.client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

    def evaluate(self, resume: dict, job_requirements: str = "") -> dict:
        """Evaluate a candidate's resume against job requirements.

        Returns:
            {score: 0-100, strengths: [], weaknesses: [], recommendation: str, summary: str}
        """
        if not self.client:
            return self._fallback_evaluate(resume)

        jd = job_requirements or DEFAULT_JD
        resume_text = self._format_resume(resume)

        response = self.client.messages.create(
            model=EVAL_MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": f"""你是一个专业的招聘评估助手。请根据以下岗位要求评估候选人简历。

## 岗位要求
{jd}

## 候选人简历
{resume_text}

请以 JSON 格式返回评估结果，包含以下字段：
- score: 匹配度分数 (0-100)
- strengths: 优势列表 (string[])
- weaknesses: 劣势/风险列表 (string[])
- recommendation: "strong_yes" | "yes" | "maybe" | "no"
- summary: 一句话总结（中文）

只返回 JSON，不要其他内容。"""
            }],
        )

        try:
            text = response.content[0].text.strip()
            # Extract JSON from response (handle markdown code blocks)
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            return json.loads(text)
        except (json.JSONDecodeError, IndexError):
            return {
                "score": 0,
                "strengths": [],
                "weaknesses": ["AI 评估解析失败"],
                "recommendation": "maybe",
                "summary": response.content[0].text[:200] if response.content else "评估失败",
            }

    def _format_resume(self, resume: dict) -> str:
        """Format resume dict into readable text."""
        parts = []
        if resume.get("name"):
            parts.append(f"姓名：{resume['name']}")
        if resume.get("education"):
            edu = resume["education"]
            if isinstance(edu, list):
                parts.append(f"教育：{'; '.join(edu)}")
            else:
                parts.append(f"教育：{edu}")
        if resume.get("skills"):
            parts.append(f"技能：{', '.join(resume['skills'])}")
        if resume.get("work_history"):
            parts.append("工作经历：")
            for w in resume["work_history"]:
                parts.append(f"  - {w}")
        if resume.get("project_experience"):
            parts.append("项目经验：")
            for p in resume["project_experience"]:
                parts.append(f"  - {p}")
        if resume.get("self_description"):
            parts.append(f"自我描述：{resume['self_description']}")
        if resume.get("full_text") and not resume.get("work_history"):
            parts.append(f"简历全文：{resume['full_text'][:2000]}")
        return "\n".join(parts)

    def _fallback_evaluate(self, resume: dict) -> dict:
        """Simple keyword-based evaluation when API is not available."""
        resume_text = self._format_resume(resume).lower()
        score = 50
        strengths = []
        weaknesses = []

        # Keyword scoring
        positive_keywords = ["产品经理", "ai", "大模型", "saas", "b端", "需求分析", "prd", "数据分析"]
        negative_keywords = ["应届", "实习", "兼职"]

        for kw in positive_keywords:
            if kw in resume_text:
                score += 5
                strengths.append(f"包含关键词: {kw}")

        for kw in negative_keywords:
            if kw in resume_text:
                score -= 10
                weaknesses.append(f"包含风险词: {kw}")

        score = max(0, min(100, score))
        recommendation = "yes" if score >= 70 else "maybe" if score >= 50 else "no"

        return {
            "score": score,
            "strengths": strengths or ["需要人工评估"],
            "weaknesses": weaknesses or ["无明显风险"],
            "recommendation": recommendation,
            "summary": f"关键词匹配分数 {score}/100（无 API 时的简易评估）",
        }
