#!/usr/bin/env python3
"""
Gemma4 Signal Validator for Kronos
====================================

Uses Ollama gemma4-heretic to validate and reason about trading signals.
Integrates with core/strategy.py StrategyResult.

Key Functions:
    - validate_signal_with_gemma4(): Validate a trading signal using Gemma4
    - Gemma4SignalValidator: Main validator class with Ollama integration

Author: Kronos Team
"""

import os
import sys
import json
import time
import signal
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum

import requests

# Add parent directory to path for imports
sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.strategy import StrategyResult, Signal, SignalType, SignalStrength

logger = logging.getLogger('kronos.gemma4_validator')


class ValidationStatus(Enum):
    """Status of signal validation."""
    VALID = "valid"
    INVALID = "invalid"
    UNCERTAIN = "uncertain"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


@dataclass
class ValidationResult:
    """Result of Gemma4 signal validation."""
    status: ValidationStatus
    reasoning: str
    confidence: float = 0.0
    validation_errors: List[str] = field(default_factory=list)
    gemma_response: str = ""
    processing_time_ms: float = 0.0
    raw_gemma_output: Optional[str] = None


class TimeoutError(Exception):
    """Raised when a operation times out."""
    pass


def _timeout_handler(signum, frame):
    """Signal handler for SIGALRM timeout."""
    raise TimeoutError("Gemma4 validation timed out after 5 seconds")


class Gemma4SignalValidator:
    """
    Signal validator using Ollama gemma4-heretic model.
    
    Validates trading signals by sending them to the Gemma4 model for
    reasoning and validation. Includes timeout handling and Ollama
    availability checks.
    """
    
    OLLAMA_BASE_URL = "http://localhost:11434"
    MODEL_NAME = "gemma4-heretic"
    TIMEOUT_SECONDS = 5
    
    def __init__(self, base_url: Optional[str] = None, model_name: Optional[str] = None):
        """
        Initialize the Gemma4 signal validator.
        
        Args:
            base_url: Ollama base URL (defaults to localhost:11434)
            model_name: Model name to use (defaults to gemma4-heretic)
        """
        self.base_url = base_url or self.OLLAMA_BASE_URL
        self.model_name = model_name or self.MODEL_NAME
        self._available = None  # Cached availability status
        
    def check_ollama(self) -> Dict[str, Any]:
        """
        Check if Ollama is available and the gemma4-heretic model is installed.
        
        Returns:
            Dictionary with availability status and model info:
            {
                "available": bool,
                "model_installed": bool,
                "model_name": str,
                "error": Optional[str]
            }
        """
        result = {
            "available": False,
            "model_installed": False,
            "model_name": self.model_name,
            "error": None
        }
        
        try:
            # Check if Ollama service is running
            response = requests.get(f"{self.base_url}/api/tags", timeout=3)
            if response.status_code == 200:
                result["available"] = True
                
                # Check if gemma4-heretic model is installed
                models = response.json().get("models", [])
                model_names = [m.get("name", "").lower() for m in models]
                
                # Check for exact match or partial match
                for name in model_names:
                    if self.model_name.lower() in name or "gemma4" in name:
                        result["model_installed"] = True
                        break
                        
                if not result["model_installed"]:
                    result["error"] = f"Model '{self.model_name}' not found. Available: {model_names}"
            else:
                result["error"] = f"Ollama returned status {response.status_code}"
                
        except requests.exceptions.ConnectionError:
            result["error"] = "Cannot connect to Ollama. Is it running?"
        except requests.exceptions.Timeout:
            result["error"] = "Ollama connection timed out"
        except Exception as e:
            result["error"] = f"Error checking Ollama: {str(e)}"
            
        self._available = result["available"] and result["model_installed"]
        return result
    
    def _call_ollama(self, prompt: str) -> Optional[str]:
        """
        Call Ollama API with the given prompt.
        
        Args:
            prompt: The prompt to send to the model
            
        Returns:
            Model response string or None if failed
            
        Raises:
            TimeoutError: If the request exceeds TIMEOUT_SECONDS
        """
        # Set up timeout using SIGALRM
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(self.TIMEOUT_SECONDS)
        
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model_name,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.3,
                        "num_predict": 512
                    }
                },
                timeout=self.TIMEOUT_SECONDS + 1
            )
            
            if response.status_code == 200:
                return response.json().get("response", "")
            else:
                logger.error(f"Ollama returned status {response.status_code}: {response.text}")
                return None
                
        except TimeoutError:
            logger.warning(f"Ollama request timed out after {self.TIMEOUT_SECONDS} seconds")
            raise
        except requests.exceptions.Timeout:
            logger.warning(f"Ollama request timed out")
            raise TimeoutError(f"Request to Ollama timed out")
        except Exception as e:
            logger.error(f"Error calling Ollama: {str(e)}")
            return None
        finally:
            # Cancel the alarm
            signal.alarm(0)
    
    def _build_validation_prompt(self, strategy_result: StrategyResult) -> str:
        """
        Build a validation prompt from a StrategyResult.
        
        Args:
            strategy_result: The strategy result to validate
            
        Returns:
            Formatted prompt string for Gemma4
        """
        signal_data = strategy_result.signal
        if signal_data:
            signal_info = f"""
Signal Type: {signal_data.signal_type.value if hasattr(signal_data.signal_type, 'value') else signal_data.signal_type}
Direction: {signal_data.direction.value if hasattr(signal_data.direction, 'value') else signal_data.direction}
Strength: {signal_data.strength.value if hasattr(signal_data.strength, 'value') else signal_data.strength}
Confidence: {signal_data.confidence}
Entry Price: {signal_data.entry_price}
Stop Loss: {signal_data.stop_loss}
Take Profit: {signal_data.take_profit}
"""
        else:
            signal_info = "No signal (hold/neutral)"
        
        indicators_str = json.dumps(strategy_result.indicators, indent=2)
        
        prompt = f"""You are a trading signal validator analyzing a potential trade for {strategy_result.symbol}.

{signal_info}

Technical Indicators:
{indicators_str}

Timestamp: {strategy_result.timestamp}

Your task is to validate this trading signal. Consider:
1. Is the signal direction consistent with the indicators?
2. Is the risk/reward ratio favorable?
3. Are there any red flags or inconsistencies?
4. Should this trade be taken or skipped?

Respond with a JSON object containing:
{{
    "valid": true/false,
    "reasoning": "Brief explanation of your validation",
    "confidence": 0.0-1.0 (how confident you are in this validation),
    "warnings": ["any warnings or concerns"]
}}

Respond ONLY with the JSON object, no other text."""
        
        return prompt
    
    def _parse_gemma_response(self, response: str) -> Dict[str, Any]:
        """
        Parse Gemma4's JSON response.
        
        Args:
            response: Raw response string from Gemma4
            
        Returns:
            Parsed dictionary with validation data
        """
        result = {
            "valid": False,
            "reasoning": "Failed to parse Gemma4 response",
            "confidence": 0.0,
            "warnings": []
        }
        
        try:
            # Try to find JSON in the response
            json_start = response.find('{')
            json_end = response.rfind('}') + 1
            
            if json_start != -1 and json_end > json_start:
                json_str = response[json_start:json_end]
                parsed = json.loads(json_str)
                
                result["valid"] = parsed.get("valid", False)
                result["reasoning"] = parsed.get("reasoning", "")
                result["confidence"] = float(parsed.get("confidence", 0.0))
                result["warnings"] = parsed.get("warnings", [])
            else:
                # Fallback: try to extract key information using patterns
                response_lower = response.lower()
                
                if "valid" in response_lower and "true" in response_lower:
                    result["valid"] = True
                elif "invalid" in response_lower or "false" in response_lower:
                    result["valid"] = False
                    
                # Try to extract confidence
                import re
                conf_match = re.search(r'confidence[:\s]+(\d+\.?\d*)', response, re.IGNORECASE)
                if conf_match:
                    result["confidence"] = float(conf_match.group(1))
                    
                result["reasoning"] = response[:500] if response else "No response from model"
                
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON: {e}")
            result["reasoning"] = f"Parse error: {str(e)}. Response: {response[:200]}"
        except Exception as e:
            logger.error(f"Error parsing Gemma response: {e}")
            result["reasoning"] = f"Error: {str(e)}"
            
        return result
    
    def validate_signal(self, strategy_result: StrategyResult) -> ValidationResult:
        """
        Validate a trading signal using Gemma4.
        
        Args:
            strategy_result: The StrategyResult to validate
            
        Returns:
            ValidationResult with Gemma4's validation
        """
        start_time = time.time()
        result = ValidationResult(
            status=ValidationStatus.ERROR,
            reasoning="Unknown error"
        )
        
        # Check availability first
        if not self._available:
            availability = self.check_ollama()
            if not availability["available"]:
                result.status = ValidationStatus.UNAVAILABLE
                result.reasoning = f"Ollama not available: {availability['error']}"
                result.validation_errors.append(availability['error'] or "Ollama service unavailable")
                return result
            if not availability["model_installed"]:
                result.status = ValidationStatus.UNAVAILABLE
                result.reasoning = f"Model not installed: {availability['error']}"
                result.validation_errors.append(availability['error'] or "Model not installed")
                return result
        
        try:
            # Build prompt
            prompt = self._build_validation_prompt(strategy_result)
            result.raw_gemma_output = prompt  # Store for debugging
            
            # Call Ollama with timeout
            response = self._call_ollama(prompt)
            result.gemma_response = response or ""
            
            # Parse response
            if response:
                parsed = self._parse_gemma_response(response)
                result.confidence = parsed["confidence"]
                result.reasoning = parsed["reasoning"]
                result.validation_errors = parsed["warnings"]
                
                if parsed["valid"]:
                    result.status = ValidationStatus.VALID
                else:
                    result.status = ValidationStatus.INVALID
            else:
                result.status = ValidationStatus.ERROR
                result.reasoning = "No response from Gemma4"
                result.validation_errors.append("Empty response from model")
                
        except TimeoutError:
            result.status = ValidationStatus.ERROR
            result.reasoning = f"Gemma4 validation timed out after {self.TIMEOUT_SECONDS} seconds"
            result.validation_errors.append("Timeout error")
        except Exception as e:
            result.status = ValidationStatus.ERROR
            result.reasoning = f"Validation error: {str(e)}"
            result.validation_errors.append(str(e))
            logger.exception("Error during Gemma4 validation")
        
        result.processing_time_ms = (time.time() - start_time) * 1000
        return result
    
    def validate_signal_with_gemma4(self, strategy_result: StrategyResult) -> ValidationResult:
        """
        Validate a trading signal using Gemma4 (convenience function).
        
        This is the main entry point for validating signals with Gemma4.
        
        Args:
            strategy_result: The StrategyResult to validate
            
        Returns:
            ValidationResult with Gemma4's validation
        """
        return self.validate_signal(strategy_result)


def validate_signal_with_gemma4(strategy_result: StrategyResult) -> ValidationResult:
    """
    Convenience function to validate a signal using Gemma4.
    
    Args:
        strategy_result: The StrategyResult to validate
        
    Returns:
        ValidationResult with Gemma4's validation
    """
    validator = Gemma4SignalValidator()
    return validator.validate_signal(strategy_result)


# Allow running as a standalone test
if __name__ == "__main__":
    import logging
    
    # Configure basic logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print("Testing Gemma4SignalValidator...")
    print("=" * 50)
    
    validator = Gemma4SignalValidator()
    
    # Check Ollama availability
    print("\nChecking Ollama availability...")
    status = validator.check_ollama()
    print(f"Ollama available: {status['available']}")
    print(f"Model installed: {status['model_installed']}")
    if status['error']:
        print(f"Error: {status['error']}")
    
    if status['available'] and status['model_installed']:
        print("\nRunning test validation...")
        
        # Create a mock strategy result for testing
        from core.strategy import Signal, SignalType, SignalStrength, Direction
        
        mock_signal = Signal(
            signal_type=SignalType.ALPHA,
            direction=Direction.LONG,
            strength=SignalStrength.MODERATE,
            confidence=0.75,
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit=52000.0
        )
        
        mock_result = StrategyResult(
            signal=mock_signal,
            symbol="BTC/USDT",
            indicators={
                "rsi": 45.5,
                "adx": 28.3,
                "macd": 150.2,
                "volume_ratio": 1.3
            },
            valid=True,
            validation_errors=[]
        )
        
        validation = validator.validate_signal(mock_result)
        print(f"\nValidation Status: {validation.status.value}")
        print(f"Confidence: {validation.confidence}")
        print(f"Reasoning: {validation.reasoning}")
        print(f"Processing Time: {validation.processing_time_ms:.2f}ms")
        if validation.validation_errors:
            print(f"Warnings: {validation.validation_errors}")
    else:
        print("\nSkipping validation test - Ollama or model not available")
