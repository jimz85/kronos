"""
Enhanced Gemma4 Output Parser for Kronos

Multi-method parsing for Gemma4 model outputs including:
- Content extraction
- Thinking/process extraction
- Delimiter-based parsing
- Fuzzy matching parsing

Author: Kronos Team
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
import re
import json
from collections import defaultdict


class ParseMethod(Enum):
    """Supported parsing methods."""
    CONTENT = "content"
    THINKING = "thinking"
    DELIMITER = "delimiter"
    FUZZY = "fuzzy"


class DecisionType(Enum):
    """Decision types from Gemma4 output."""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    UNKNOWN = "unknown"


@dataclass
class GemmaDecision:
    """Structured decision output from Gemma4 parser."""
    decision_type: DecisionType = DecisionType.UNKNOWN
    confidence: float = 0.0
    raw_content: str = ""
    thinking: Optional[str] = None
    reasoning: Optional[str] = None
    symbols: List[str] = field(default_factory=list)
    position_size: Optional[float] = None
    risk_level: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    parse_method: ParseMethod = ParseMethod.CONTENT
    parse_success: bool = False


class GemmaOutputParser:
    """
    Enhanced parser for Gemma4 model outputs.
    
    Supports multiple parsing strategies:
    - Content: Direct content extraction
    - Thinking: Extract reasoning/thinking blocks
    - Delimiter: Parse using delimiters like <output>, </output>
    - Fuzzy: Fuzzy matching for flexible parsing
    """
    
    def __init__(self):
        """Initialize the parser with statistics tracking."""
        self._stats = {
            "total_parses": 0,
            "successful_parses": 0,
            "failed_parses": 0,
            "method_stats": defaultdict(lambda: {"attempts": 0, "successes": 0})
        }
        
        # Common patterns for Gemma4 output
        self._patterns = {
            "buy": re.compile(r'\b(buy|long|bullish|accumulate)\b', re.IGNORECASE),
            "sell": re.compile(r'\b(sell|short|bearish|distribute)\b', re.IGNORECASE),
            "hold": re.compile(r'\b(hold|neutral|wait|skip)\b', re.IGNORECASE),
            "confidence": re.compile(r'confidence[:\s]+(\d+\.?\d*)', re.IGNORECASE),
            "symbol": re.compile(r'\b[A-Z]{2,10}\b'),
            "position_size": re.compile(r'position[:\s]*(\d+\.?\d*)%?', re.IGNORECASE),
            "risk": re.compile(r'risk[:\s]*(low|medium|high)', re.IGNORECASE),
            "thinking_open": re.compile(r'<thinking>(.*?)</thinking>', re.DOTALL | re.IGNORECASE),
            "thinking_alt": re.compile(r'\[THINKING\](.*?)\[/THINKING\]', re.DOTALL | re.IGNORECASE),
            "output_open": re.compile(r'<output>(.*?)</output>', re.DOTALL | re.IGNORECASE),
            "output_alt": re.compile(r'\[OUTPUT\](.*?)\[/OUTPUT\]', re.DOTALL | re.IGNORECASE),
            "reasoning": re.compile(r'(?:because|reason|since|therefore|thus)[:\s]+(.+?)(?:\.|$)', re.IGNORECASE),
        }
    
    def parse(self, output: str, method: ParseMethod = ParseMethod.CONTENT) -> GemmaDecision:
        """
        Parse Gemma4 output using specified method.
        
        Args:
            output: Raw Gemma4 model output string
            method: Parsing method to use
            
        Returns:
            GemmaDecision with structured parsed output
        """
        self._stats["total_parses"] += 1
        self._stats["method_stats"][method.value]["attempts"] += 1
        
        decision = GemmaDecision(parse_method=method)
        
        try:
            if method == ParseMethod.CONTENT:
                decision = self._parse_content(output)
            elif method == ParseMethod.THINKING:
                decision = self._parse_thinking(output)
            elif method == ParseMethod.DELIMITER:
                decision = self._parse_delimiter(output)
            elif method == ParseMethod.FUZZY:
                decision = self._parse_fuzzy(output)
            else:
                decision = self._parse_content(output)
            
            decision.parse_success = True
            self._stats["successful_parses"] += 1
            self._stats["method_stats"][method.value]["successes"] += 1
            
        except Exception as e:
            decision.parse_success = False
            decision.metadata["error"] = str(e)
            self._stats["failed_parses"] += 1
        
        return decision
    
    def _parse_content(self, output: str) -> GemmaDecision:
        """Parse using direct content extraction."""
        decision = GemmaDecision(parse_method=ParseMethod.CONTENT)
        decision.raw_content = output.strip()
        
        # Extract decision type
        decision.decision_type = self._extract_decision_type(output)
        
        # Extract confidence
        decision.confidence = self._extract_confidence(output)
        
        # Extract symbols
        decision.symbols = self._extract_symbols(output)
        
        # Extract position size
        decision.position_size = self._extract_position_size(output)
        
        # Extract risk level
        decision.risk_level = self._extract_risk_level(output)
        
        # Extract reasoning
        decision.reasoning = self._extract_reasoning(output)
        
        return decision
    
    def _parse_thinking(self, output: str) -> GemmaDecision:
        """Parse by extracting thinking/reasoning blocks."""
        decision = GemmaDecision(parse_method=ParseMethod.THINKING)
        decision.raw_content = output.strip()
        
        # Try to extract thinking content
        thinking_match = self._patterns["thinking_open"].search(output)
        if not thinking_match:
            thinking_match = self._patterns["thinking_alt"].search(output)
        
        if thinking_match:
            decision.thinking = thinking_match.group(1).strip()
            # Use thinking content for further extraction
            extract_from = decision.thinking
        else:
            extract_from = output
        
        # Extract decision type from thinking or full output
        decision.decision_type = self._extract_decision_type(extract_from)
        decision.confidence = self._extract_confidence(extract_from)
        decision.symbols = self._extract_symbols(extract_from)
        decision.position_size = self._extract_position_size(extract_from)
        decision.risk_level = self._extract_risk_level(extract_from)
        decision.reasoning = self._extract_reasoning(extract_from)
        
        return decision
    
    def _parse_delimiter(self, output: str) -> GemmaDecision:
        """Parse using delimiter-based extraction."""
        decision = GemmaDecision(parse_method=ParseMethod.DELIMITER)
        decision.raw_content = output.strip()
        
        # Try to extract content between delimiters
        delimiter_match = self._patterns["output_open"].search(output)
        if not delimiter_match:
            delimiter_match = self._patterns["output_alt"].search(output)
        
        if delimiter_match:
            content = delimiter_match.group(1).strip()
        else:
            # Try thinking delimiters
            thinking_match = self._patterns["thinking_open"].search(output)
            if not thinking_match:
                thinking_match = self._patterns["thinking_alt"].search(output)
            
            if thinking_match:
                decision.thinking = thinking_match.group(1).strip()
                content = output.replace(thinking_match.group(0), "").strip()
            else:
                content = output.strip()
        
        decision.decision_type = self._extract_decision_type(content)
        decision.confidence = self._extract_confidence(content)
        decision.symbols = self._extract_symbols(content)
        decision.position_size = self._extract_position_size(content)
        decision.risk_level = self._extract_risk_level(content)
        decision.reasoning = self._extract_reasoning(content)
        
        return decision
    
    def _parse_fuzzy(self, output: str) -> GemmaDecision:
        """Parse using fuzzy matching for flexible extraction."""
        decision = GemmaDecision(parse_method=ParseMethod.FUZZY)
        decision.raw_content = output.strip()
        
        # Fuzzy extraction of decision type
        decision.decision_type = self._fuzzy_extract_decision_type(output)
        
        # Extract confidence
        decision.confidence = self._fuzzy_extract_confidence(output)
        
        # Extract symbols
        decision.symbols = self._fuzzy_extract_symbols(output)
        
        # Extract position size
        decision.position_size = self._fuzzy_extract_position_size(output)
        
        # Extract risk level
        decision.risk_level = self._fuzzy_extract_risk_level(output)
        
        # Extract reasoning
        decision.reasoning = self._fuzzy_extract_reasoning(output)
        
        return decision
    
    def _extract_decision_type(self, text: str) -> DecisionType:
        """Extract decision type using pattern matching."""
        buy_matches = self._patterns["buy"].findall(text)
        sell_matches = self._patterns["sell"].findall(text)
        hold_matches = self._patterns["hold"].findall(text)
        
        # Count matches and determine decision
        buy_count = len(buy_matches)
        sell_count = len(sell_matches)
        hold_count = len(hold_matches)
        
        max_count = max(buy_count, sell_count, hold_count)
        
        if max_count == 0:
            return DecisionType.UNKNOWN
        elif buy_count == max_count:
            return DecisionType.BUY
        elif sell_count == max_count:
            return DecisionType.SELL
        else:
            return DecisionType.HOLD
    
    def _fuzzy_extract_decision_type(self, text: str) -> DecisionType:
        """Fuzzy extraction of decision type with partial matching."""
        text_lower = text.lower()
        
        # Weighted scoring
        scores = {
            DecisionType.BUY: 0,
            DecisionType.SELL: 0,
            DecisionType.HOLD: 0
        }
        
        # Buy indicators
        buy_terms = ["buy", "long", "bullish", "accumulate", "long position", "go long", "call"]
        for term in buy_terms:
            if term in text_lower:
                scores[DecisionType.BUY] += 1
                if "strong" in text_lower or "very" in text_lower:
                    scores[DecisionType.BUY] += 0.5
        
        # Sell indicators
        sell_terms = ["sell", "short", "bearish", "distribute", "short position", "go short", "put"]
        for term in sell_terms:
            if term in text_lower:
                scores[DecisionType.SELL] += 1
                if "strong" in text_lower or "very" in text_lower:
                    scores[DecisionType.SELL] += 0.5
        
        # Hold indicators
        hold_terms = ["hold", "neutral", "wait", "skip", "no position", "stay out", "pass"]
        for term in hold_terms:
            if term in text_lower:
                scores[DecisionType.HOLD] += 1
        
        # Return highest scoring decision
        if max(scores.values()) == 0:
            return DecisionType.UNKNOWN
        
        return max(scores, key=scores.get)
    
    def _extract_confidence(self, text: str) -> float:
        """Extract confidence score from text."""
        match = self._patterns["confidence"].search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass
        return 0.0
    
    def _fuzzy_extract_confidence(self, text: str) -> float:
        """Fuzzy extraction of confidence score."""
        # Try standard pattern first
        match = self._patterns["confidence"].search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass
        
        # Try percentage format
        percent_match = re.search(r'(\d+)%', text)
        if percent_match:
            try:
                return float(percent_match.group(1))
            except ValueError:
                pass
        
        return 0.0
    
    def _extract_symbols(self, text: str) -> List[str]:
        """Extract trading symbols from text."""
        matches = self._patterns["symbol"].findall(text)
        # Filter out common words that look like symbols
        common_words = {"AI", "BTC", "ETH", "USD", "API", "CEO", "IPO", "ETF", "SEC", "FDA"}
        symbols = [m for m in matches if len(m) >= 2]
        return list(set(symbols))
    
    def _fuzzy_extract_symbols(self, text: str) -> List[str]:
        """Fuzzy extraction of trading symbols."""
        # Standard extraction
        symbols = self._extract_symbols(text)
        
        # Look for symbol patterns like $BTC, BTC/USD, etc.
        dollar_pattern = re.compile(r'\$([A-Z]{2,10})')
        dollar_symbols = dollar_pattern.findall(text)
        symbols.extend(dollar_symbols)
        
        # Look for trading pair patterns
        pair_pattern = re.compile(r'([A-Z]{2,6})/[A-Z]{2,6}')
        pairs = pair_pattern.findall(text)
        symbols.extend(pairs)
        
        return list(set(symbols)) if symbols else []
    
    def _extract_position_size(self, text: str) -> Optional[float]:
        """Extract position size from text."""
        match = self._patterns["position_size"].search(text)
        if match:
            try:
                value = float(match.group(1))
                # Convert percentage to decimal if needed
                if value > 1:
                    value = value / 100
                return value
            except ValueError:
                pass
        return None
    
    def _fuzzy_extract_position_size(self, text: str) -> Optional[float]:
        """Fuzzy extraction of position size."""
        # Try standard extraction first
        pos_size = self._extract_position_size(text)
        if pos_size is not None:
            return pos_size
        
        # Try "X% of portfolio" pattern
        portfolio_match = re.search(r'(\d+\.?\d*)\s*%?\s*(?:of\s+)?portfolio', text, re.IGNORECASE)
        if portfolio_match:
            try:
                value = float(portfolio_match.group(1))
                if value > 1:
                    value = value / 100
                return value
            except ValueError:
                pass
        
        return None
    
    def _extract_risk_level(self, text: str) -> Optional[str]:
        """Extract risk level from text."""
        match = self._patterns["risk"].search(text)
        if match:
            return match.group(1).lower()
        return None
    
    def _fuzzy_extract_risk_level(self, text: str) -> Optional[str]:
        """Fuzzy extraction of risk level."""
        # Try standard extraction first
        risk = self._extract_risk_level(text)
        if risk:
            return risk
        
        text_lower = text.lower()
        
        # Look for risk-related phrases
        if any(term in text_lower for term in ["high risk", "risky", "aggressive", "maximum risk"]):
            return "high"
        elif any(term in text_lower for term in ["medium risk", "moderate", "balanced"]):
            return "medium"
        elif any(term in text_lower for term in ["low risk", "conservative", "safe", "minimal risk"]):
            return "low"
        
        return None
    
    def _extract_reasoning(self, text: str) -> Optional[str]:
        """Extract reasoning from text."""
        match = self._patterns["reasoning"].search(text)
        if match:
            return match.group(1).strip()
        return None
    
    def _fuzzy_extract_reasoning(self, text: str) -> Optional[str]:
        """Fuzzy extraction of reasoning."""
        # Try standard extraction first
        reasoning = self._extract_reasoning(text)
        if reasoning:
            return reasoning
        
        # Look for common reasoning patterns
        reasoning_starts = [
            "because", "reason:", "the reason", "due to", "since",
            "based on", "given that", "considering", "therefore"
        ]
        
        text_lower = text.lower()
        for start in reasoning_starts:
            idx = text_lower.find(start)
            if idx != -1:
                # Extract sentence from this point
                sentence_end = text.find(".", idx)
                if sentence_end != -1:
                    return text[idx:sentence_end + 1].strip()
                else:
                    return text[idx:idx + 200].strip()
        
        return None
    
    def parse_all_methods(self, output: str) -> Dict[ParseMethod, GemmaDecision]:
        """
        Parse output using all methods and return results.
        
        Args:
            output: Raw Gemma4 model output string
            
        Returns:
            Dictionary mapping ParseMethod to GemmaDecision results
        """
        results = {}
        for method in ParseMethod:
            results[method] = self.parse(output, method)
        return results
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get parsing statistics.
        
        Returns:
            Dictionary containing:
            - total_parses: Total number of parse attempts
            - successful_parses: Number of successful parses
            - failed_parses: Number of failed parses
            - success_rate: Percentage of successful parses
            - method_stats: Statistics per parsing method
        """
        total = self._stats["total_parses"]
        success = self._stats["successful_parses"]
        failed = self._stats["failed_parses"]
        
        success_rate = (success / total * 100) if total > 0 else 0.0
        
        method_stats = {}
        for method, stats in self._stats["method_stats"].items():
            attempts = stats["attempts"]
            successes = stats["successes"]
            method_stats[method] = {
                "attempts": attempts,
                "successes": successes,
                "success_rate": (successes / attempts * 100) if attempts > 0 else 0.0
            }
        
        return {
            "total_parses": total,
            "successful_parses": success,
            "failed_parses": failed,
            "success_rate": round(success_rate, 2),
            "method_stats": method_stats
        }
    
    def reset_stats(self) -> None:
        """Reset all statistics counters."""
        self._stats = {
            "total_parses": 0,
            "successful_parses": 0,
            "failed_parses": 0,
            "method_stats": defaultdict(lambda: {"attempts": 0, "successes": 0})
        }
    
    def to_dict(self, decision: GemmaDecision) -> Dict[str, Any]:
        """
        Convert GemmaDecision to dictionary.
        
        Args:
            decision: GemmaDecision to convert
            
        Returns:
            Dictionary representation of decision
        """
        return {
            "decision_type": decision.decision_type.value,
            "confidence": decision.confidence,
            "raw_content": decision.raw_content,
            "thinking": decision.thinking,
            "reasoning": decision.reasoning,
            "symbols": decision.symbols,
            "position_size": decision.position_size,
            "risk_level": decision.risk_level,
            "metadata": decision.metadata,
            "parse_method": decision.parse_method.value,
            "parse_success": decision.parse_success
        }
    
    def to_json(self, decision: GemmaDecision) -> str:
        """
        Convert GemmaDecision to JSON string.
        
        Args:
            decision: GemmaDecision to convert
            
        Returns:
            JSON string representation
        """
        return json.dumps(self.to_dict(decision), indent=2)


# Convenience function for quick parsing
def quick_parse(output: str, method: ParseMethod = ParseMethod.CONTENT) -> GemmaDecision:
    """
    Quick parse function for one-off parsing.
    
    Args:
        output: Raw Gemma4 model output string
        method: Parsing method to use
        
    Returns:
        GemmaDecision with structured parsed output
    """
    parser = GemmaOutputParser()
    return parser.parse(output, method)


if __name__ == "__main__":
    # Test the parser
    test_output = """
    [THINKING]
    Based on the technical analysis and market conditions,
    I recommend buying BTC with high confidence.
    [/THINKING]
    
    [OUTPUT]
    Decision: BUY
    Confidence: 85%
    Symbol: BTC
    Position: 10% of portfolio
    Risk: medium
    Because the trend is bullish and RSI shows oversold conditions.
    [/OUTPUT]
    """
    
    parser = GemmaOutputParser()
    
    # Test all methods
    print("Testing Gemma4 Parser")
    print("=" * 50)
    
    for method in ParseMethod:
        result = parser.parse(test_output, method)
        print(f"\n{method.value.upper()} Parse:")
        print(f"  Decision: {result.decision_type.value}")
        print(f"  Confidence: {result.confidence}")
        print(f"  Symbols: {result.symbols}")
        print(f"  Success: {result.parse_success}")
    
    print("\n" + "=" * 50)
    print("Statistics:")
    stats = parser.get_stats()
    for key, value in stats.items():
        print(f"  {key}: {value}")
