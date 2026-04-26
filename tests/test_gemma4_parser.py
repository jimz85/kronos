"""
Tests for core/gemma4_parser.py
==============================

Unit tests for the Gemma4 output parser.
"""

import pytest
from core.gemma4_parser import (
    GemmaOutputParser,
    GemmaDecision,
    ParseMethod,
    DecisionType
)


class TestGemmaOutputParser:
    """Tests for GemmaOutputParser class."""

    def test_parser_initialization(self, parser):
        """Test parser initializes with correct default state."""
        stats = parser.get_stats()
        assert stats['total_parses'] == 0
        assert stats['successful_parses'] == 0
        assert stats['failed_parses'] == 0
        assert stats['success_rate'] == 0.0

    def test_parse_content_method(self, parser, sample_gemma_output):
        """Test CONTENT parsing method."""
        decision = parser.parse(sample_gemma_output, ParseMethod.CONTENT)
        assert decision.parse_method == ParseMethod.CONTENT
        assert decision.parse_success is True
        assert decision.raw_content is not None

    def test_parse_thinking_method(self, parser, sample_gemma_output):
        """Test THINKING parsing method."""
        decision = parser.parse(sample_gemma_output, ParseMethod.THINKING)
        assert decision.parse_method == ParseMethod.THINKING
        assert decision.parse_success is True
        assert decision.thinking is not None

    def test_parse_delimiter_method(self, parser, sample_gemma_output):
        """Test DELIMITER parsing method."""
        decision = parser.parse(sample_gemma_output, ParseMethod.DELIMITER)
        assert decision.parse_method == ParseMethod.DELIMITER
        assert decision.parse_success is True

    def test_parse_fuzzy_method(self, parser, sample_gemma_output):
        """Test FUZZY parsing method."""
        decision = parser.parse(sample_gemma_output, ParseMethod.FUZZY)
        assert decision.parse_method == ParseMethod.FUZZY
        assert decision.parse_success is True

    def test_parse_all_methods(self, parser, sample_gemma_output):
        """Test parsing with all methods."""
        results = parser.parse_all_methods(sample_gemma_output)
        assert len(results) == len(ParseMethod)
        for method in ParseMethod:
            assert method in results
            assert results[method].parse_success is True


class TestGemmaDecision:
    """Tests for GemmaDecision dataclass."""

    def test_decision_default_values(self):
        """Test GemmaDecision default values."""
        decision = GemmaDecision()
        assert decision.decision_type == DecisionType.UNKNOWN
        assert decision.confidence == 0.0
        assert decision.raw_content == ""
        assert decision.thinking is None
        assert decision.reasoning is None
        assert decision.symbols == []
        assert decision.position_size is None
        assert decision.risk_level is None
        assert decision.metadata == {}
        assert decision.parse_method == ParseMethod.CONTENT
        assert decision.parse_success is False

    def test_decision_with_values(self):
        """Test GemmaDecision with custom values."""
        decision = GemmaDecision(
            decision_type=DecisionType.BUY,
            confidence=0.85,
            raw_content="Test content",
            thinking="Test thinking",
            reasoning="Test reasoning",
            symbols=["BTC", "ETH"],
            position_size=0.1,
            risk_level="medium",
            parse_method=ParseMethod.FUZZY,
            parse_success=True
        )
        assert decision.decision_type == DecisionType.BUY
        assert decision.confidence == 0.85
        assert decision.thinking == "Test thinking"
        assert decision.symbols == ["BTC", "ETH"]
        assert decision.position_size == 0.1
        assert decision.risk_level == "medium"


class TestDecisionType:
    """Tests for DecisionType enum."""

    def test_decision_types_exist(self):
        """Test all expected decision types exist."""
        assert DecisionType.BUY.value == "buy"
        assert DecisionType.SELL.value == "sell"
        assert DecisionType.HOLD.value == "hold"
        assert DecisionType.UNKNOWN.value == "unknown"


class TestParseMethod:
    """Tests for ParseMethod enum."""

    def test_parse_methods_exist(self):
        """Test all expected parse methods exist."""
        assert ParseMethod.CONTENT.value == "content"
        assert ParseMethod.THINKING.value == "thinking"
        assert ParseMethod.DELIMITER.value == "delimiter"
        assert ParseMethod.FUZZY.value == "fuzzy"


class TestParserDecisionExtraction:
    """Tests for decision type extraction."""

    def test_extract_buy_signal(self, parser):
        """Test extraction of BUY decision."""
        output = "I recommend to BUY BTC at current levels"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.decision_type == DecisionType.BUY

    def test_extract_sell_signal(self, parser):
        """Test extraction of SELL decision."""
        output = "SELL signal detected for ETH"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.decision_type == DecisionType.SELL

    def test_extract_hold_signal(self, parser):
        """Test extraction of HOLD decision."""
        output = "HOLD your current position"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.decision_type == DecisionType.HOLD

    def test_extract_unknown_signal(self, parser):
        """Test extraction with no clear signal."""
        output = "The weather is nice today"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.decision_type == DecisionType.UNKNOWN


class TestParserConfidenceExtraction:
    """Tests for confidence score extraction."""

    def test_extract_confidence_number(self, parser):
        """Test extraction of numeric confidence."""
        output = "Confidence: 0.85"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.confidence == 0.85

    def test_extract_confidence_decimal(self, parser):
        """Test extraction of decimal confidence."""
        output = "confidence: 0.75"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.confidence == 0.75

    def test_extract_no_confidence(self, parser):
        """Test with no confidence in output."""
        output = "No confidence value here"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.confidence == 0.0


class TestParserSymbolExtraction:
    """Tests for trading symbol extraction."""

    def test_extract_single_symbol(self, parser):
        """Test extraction of single symbol."""
        output = "BTC is showing bullish momentum"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert "BTC" in decision.symbols

    def test_extract_multiple_symbols(self, parser):
        """Test extraction of multiple symbols."""
        output = "BTC and ETH are both moving up"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert "BTC" in decision.symbols
        assert "ETH" in decision.symbols

    def test_extract_symbols_no_duplicates(self, parser):
        """Test that symbols are not duplicated."""
        output = "BTC BTC BTC is trending"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.symbols.count("BTC") == 1


class TestParserPositionSizeExtraction:
    """Tests for position size extraction."""

    def test_extract_position_percentage(self, parser):
        """Test extraction of position size as percentage."""
        output = "Position: 10% of portfolio"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.position_size == 0.10

    def test_extract_position_decimal(self, parser):
        """Test extraction of position size as decimal."""
        output = "position: 0.05"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.position_size == 0.05

    def test_extract_no_position(self, parser):
        """Test with no position size in output."""
        output = "No position size mentioned"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.position_size is None


class TestParserRiskLevelExtraction:
    """Tests for risk level extraction."""

    def test_extract_risk_low(self, parser):
        """Test extraction of low risk level."""
        output = "Risk: low"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.risk_level == "low"

    def test_extract_risk_medium(self, parser):
        """Test extraction of medium risk level."""
        output = "Risk: medium"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.risk_level == "medium"

    def test_extract_risk_high(self, parser):
        """Test extraction of high risk level."""
        output = "risk: high"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.risk_level == "high"

    def test_extract_no_risk(self, parser):
        """Test with no risk level in output."""
        output = "No risk mentioned"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.risk_level is None


class TestParserThinkingExtraction:
    """Tests for thinking block extraction."""

    def test_extract_thinking_tags(self, parser):
        """Test extraction with <thinking> tags."""
        output = "<thinking>Reasoning here</thinking>Some output"
        decision = parser.parse(output, ParseMethod.THINKING)
        assert decision.thinking == "Reasoning here"

    def test_extract_thinking_brackets(self, parser):
        """Test extraction with [THINKING] tags."""
        output = "[THINKING]Reasoning here[/THINKING]Some output"
        decision = parser.parse(output, ParseMethod.THINKING)
        assert decision.thinking == "Reasoning here"


class TestParserDelimiterExtraction:
    """Tests for delimiter-based extraction."""

    def test_extract_output_tags(self, parser):
        """Test extraction with <output> tags."""
        output = "<output>BUY signal</output>"
        decision = parser.parse(output, ParseMethod.DELIMITER)
        assert decision.raw_content is not None

    def test_extract_output_brackets(self, parser):
        """Test extraction with [OUTPUT] tags."""
        output = "[OUTPUT]SELL signal[/OUTPUT]"
        decision = parser.parse(output, ParseMethod.DELIMITER)
        assert decision.raw_content is not None


class TestParserStats:
    """Tests for parser statistics tracking."""

    def test_stats_update_on_parse(self, parser, sample_gemma_output):
        """Test that stats update after parsing."""
        parser.parse(sample_gemma_output, ParseMethod.CONTENT)
        stats = parser.get_stats()
        assert stats['total_parses'] == 1
        assert stats['successful_parses'] == 1
        assert stats['failed_parses'] == 0

    def test_stats_per_method(self, parser, sample_gemma_output):
        """Test stats tracked per method."""
        parser.parse(sample_gemma_output, ParseMethod.CONTENT)
        parser.parse(sample_gemma_output, ParseMethod.THINKING)
        stats = parser.get_stats()
        assert 'content' in stats['method_stats']
        assert 'thinking' in stats['method_stats']

    def test_reset_stats(self, parser, sample_gemma_output):
        """Test resetting statistics."""
        parser.parse(sample_gemma_output, ParseMethod.CONTENT)
        parser.reset_stats()
        stats = parser.get_stats()
        assert stats['total_parses'] == 0
        assert stats['successful_parses'] == 0

    def test_success_rate_calculation(self, parser, sample_gemma_output):
        """Test success rate is calculated correctly."""
        parser.parse(sample_gemma_output, ParseMethod.CONTENT)
        parser.parse(sample_gemma_output, ParseMethod.THINKING)
        stats = parser.get_stats()
        assert stats['success_rate'] == 100.0


class TestFuzzyParsing:
    """Tests for fuzzy parsing method."""

    def test_fuzzy_buy_extraction(self, parser):
        """Test fuzzy BUY signal extraction."""
        output = "I am very bullish on BTC"
        decision = parser.parse(output, ParseMethod.FUZZY)
        assert decision.decision_type == DecisionType.BUY

    def test_fuzzy_sell_extraction(self, parser):
        """Test fuzzy SELL signal extraction."""
        output = "Strong bearish sentiment for ETH"
        decision = parser.parse(output, ParseMethod.FUZZY)
        assert decision.decision_type == DecisionType.SELL

    def test_fuzzy_confidence_percentage(self, parser):
        """Test fuzzy extraction of percentage confidence."""
        output = "Confidence is at 85%"
        decision = parser.parse(output, ParseMethod.FUZZY)
        assert decision.confidence == 85.0

    def test_fuzzy_symbol_dollar(self, parser):
        """Test fuzzy extraction of $SYMBOL format."""
        output = "$BTC showing strength"
        decision = parser.parse(output, ParseMethod.FUZZY)
        assert "BTC" in decision.symbols

    def test_fuzzy_symbol_pair(self, parser):
        """Test fuzzy extraction of trading pair format."""
        output = "BTC/USD pair moving up"
        decision = parser.parse(output, ParseMethod.FUZZY)
        assert "BTC" in decision.symbols

    def test_fuzzy_risk_phrases(self, parser):
        """Test fuzzy risk level from phrases."""
        output = "This is an aggressive trade"
        decision = parser.parse(output, ParseMethod.FUZZY)
        assert decision.risk_level == "high"

    def test_fuzzy_reasoning_extraction(self, parser):
        """Test fuzzy reasoning extraction."""
        output = "I recommend this because the trend is strong"
        decision = parser.parse(output, ParseMethod.FUZZY)
        assert decision.reasoning is not None


class TestParserEdgeCases:
    """Edge case tests for parser."""

    def test_empty_output(self, parser):
        """Test parsing empty string."""
        decision = parser.parse("", ParseMethod.CONTENT)
        assert decision.parse_success is True
        assert decision.decision_type == DecisionType.UNKNOWN

    def test_unicode_content(self, parser):
        """Test parsing unicode content."""
        output = "BUY 🪙 at current levels"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.decision_type == DecisionType.BUY

    def test_malformed_tags(self, parser):
        """Test handling of malformed tags."""
        output = "<thinking>unclosed thinking<output>content"
        decision = parser.parse(output, ParseMethod.THINKING)
        assert decision.parse_success is True

    def test_very_long_output(self, parser):
        """Test parsing very long output."""
        output = "BUY " * 1000
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.decision_type == DecisionType.BUY

    def test_special_characters(self, parser):
        """Test parsing with special characters."""
        output = "Confidence: 0.85\nRisk: medium\nPosition: 5%\n<thinking>Reasoning</thinking>"
        decision = parser.parse(output, ParseMethod.CONTENT)
        assert decision.confidence == 0.85
        assert decision.risk_level == "medium"
        assert decision.position_size == 0.05
