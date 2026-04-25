"""
Kronos Evolution Engine

Evolutionary algorithm for optimizing trading strategy parameters.
Uses genetic algorithms to evolve strategy configurations over time.

Based on kronos_v2 architecture patterns.
"""

import os
import sys
import json
import random
import logging
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime
import copy

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class Gene:
    """Individual gene representing a strategy parameter."""
    name: str
    value: float
    min_val: float
    max_val: float
    mutation_scale: float = 0.1
    
    def mutate(self, rate: float = 0.1) -> 'Gene':
        """Mutate this gene's value."""
        if random.random() < rate:
            delta = random.gauss(0, self.mutation_scale * (self.max_val - self.min_val))
            new_value = self.value + delta
            new_value = max(self.min_val, min(self.max_val, new_value))
            return Gene(self.name, new_value, self.min_val, self.max_val, self.mutation_scale)
        return copy.deepcopy(self)
    
    def crossover(self, other: 'Gene') -> Tuple['Gene', 'Gene']:
        """Single-point crossover with another gene."""
        alpha = random.random()
        v1 = alpha * self.value + (1 - alpha) * other.value
        v2 = (1 - alpha) * self.value + alpha * other.value
        v1 = max(self.min_val, min(self.max_val, v1))
        v2 = max(self.min_val, min(self.max_val, v2))
        return (
            Gene(self.name, v1, self.min_val, self.max_val, self.mutation_scale),
            Gene(self.name, v2, self.min_val, self.max_val, self.mutation_scale)
        )


@dataclass
class Chromosome:
    """Complete set of genes representing a strategy configuration."""
    genes: List[Gene]
    fitness: float = 0.0
    generation: int = 0
    id: str = ""
    
    def __post_init__(self):
        if not self.id:
            self.id = f"{datetime.now().strftime('%H%M%S')}_{random.randint(1000, 9999)}"
    
    def get_value(self, name: str) -> Optional[float]:
        """Get gene value by name."""
        for gene in self.genes:
            if gene.name == name:
                return gene.value
        return None
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            'id': self.id,
            'fitness': self.fitness,
            'generation': self.generation,
            'genes': {g.name: g.value for g in self.genes}
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Chromosome':
        """Deserialize from dictionary."""
        genes = [
            Gene(name, value, 0, 1)  # Defaults, should be overridden
            for name, value in data.get('genes', {}).items()
        ]
        return cls(
            genes=genes,
            fitness=data.get('fitness', 0.0),
            generation=data.get('generation', 0),
            id=data.get('id', '')
        )


class FitnessFunction:
    """Base class for strategy fitness evaluation."""
    
    def evaluate(self, chromosome: Chromosome) -> float:
        """Evaluate fitness of a chromosome. Higher is better."""
        raise NotImplementedError


class SharpeFitness(FitnessFunction):
    """Fitness based on Sharpe-like ratio."""
    
    def evaluate(self, chromosome: Chromosome) -> float:
        """Simple fitness: reward high return, penalize high volatility."""
        params = {g.name: g.value for g in chromosome.genes}
        
        # Mock evaluation for demo
        rsi_period = params.get('rsi_period', 14)
        atr_multiplier = params.get('atr_multiplier', 2.0)
        
        # Simulated fitness based on parameter quality
        score = 1.0
        score *= (1.0 - abs(rsi_period - 14) / 20)  # Optimal around 14
        score *= (1.0 - abs(atr_multiplier - 2.0) / 3)  # Optimal around 2.0
        
        # Add noise to simulate real evaluation
        score += random.gauss(0, 0.1)
        
        return max(0.0, min(2.0, score))


class EvolutionEngine:
    """
    Evolutionary algorithm engine for strategy parameter optimization.
    
    Uses tournament selection, uniform crossover, and gaussian mutation.
    """
    
    def __init__(
        self,
        population_size: int = 50,
        elite_count: int = 5,
        mutation_rate: float = 0.1,
        crossover_rate: float = 0.7,
        fitness_fn: Optional[FitnessFunction] = None
    ):
        self.population_size = population_size
        self.elite_count = elite_count
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.fitness_fn = fitness_fn or SharpeFitness()
        
        self.population: List[Chromosome] = []
        self.generation = 0
        self.best_chromosome: Optional[Chromosome] = None
        self.history: List[Dict[str, Any]] = []
        
    def initialize_population(self, param_spaces: Dict[str, Dict[str, float]]) -> None:
        """Initialize random population from parameter spaces."""
        self.population = []
        
        for _ in range(self.population_size):
            genes = []
            for name, space in param_spaces.items():
                value = random.uniform(space['min'], space['max'])
                gene = Gene(
                    name=name,
                    value=value,
                    min_val=space['min'],
                    max_val=space['max'],
                    mutation_scale=space.get('scale', 0.1)
                )
                genes.append(gene)
            
            chromosome = Chromosome(genes=genes, generation=0)
            self.population.append(chromosome)
        
        logger.info(f"Initialized population with {len(self.population)} chromosomes")
    
    def evaluate_population(self) -> None:
        """Evaluate fitness for all chromosomes."""
        for chrom in self.population:
            chrom.fitness = self.fitness_fn.evaluate(chrom)
            chrom.generation = self.generation
        
        # Track best
        self.population.sort(key=lambda c: c.fitness, reverse=True)
        
        if self.best_chromosome is None or self.population[0].fitness > self.best_chromosome.fitness:
            self.best_chromosome = copy.deepcopy(self.population[0])
        
        # Log stats
        fitnesses = [c.fitness for c in self.population]
        logger.info(
            f"Gen {self.generation}: "
            f"best={max(fitnesses):.4f}, "
            f"avg={sum(fitnesses)/len(fitnesses):.4f}, "
            f"worst={min(fitnesses):.4f}"
        )
        
        self.history.append({
            'generation': self.generation,
            'best_fitness': max(fitnesses),
            'avg_fitness': sum(fitnesses) / len(fitnesses),
            'best_params': self.population[0].to_dict()['genes']
        })
    
    def _tournament_select(self, k: int = 3) -> Chromosome:
        """Tournament selection."""
        tournament = random.sample(self.population, min(k, len(self.population)))
        return max(tournament, key=lambda c: c.fitness)
    
    def _crossover(self, parent1: Chromosome, parent2: Chromosome) -> Tuple[Chromosome, Chromosome]:
        """Uniform crossover between two parents."""
        if random.random() > self.crossover_rate:
            return copy.deepcopy(parent1), copy.deepcopy(parent2)
        
        genes1, genes2 = [], []
        for g1, g2 in zip(parent1.genes, parent2.genes):
            if random.random() < 0.5:
                genes1.append(copy.deepcopy(g1))
                genes2.append(copy.deepcopy(g2))
            else:
                genes1.append(copy.deepcopy(g2))
                genes2.append(copy.deepcopy(g1))
        
        return (
            Chromosome(genes=genes1, generation=self.generation + 1),
            Chromosome(genes=genes2, generation=self.generation + 1)
        )
    
    def _mutate(self, chromosome: Chromosome) -> Chromosome:
        """Apply mutation to chromosome."""
        new_genes = [gene.mutate(self.mutation_rate) for gene in chromosome.genes]
        return Chromosome(genes=new_genes, generation=chromosome.generation)
    
    def evolve(self) -> None:
        """Run one generation of evolution."""
        self.evaluate_population()
        
        new_population = []
        
        # Elitism: keep best chromosomes
        new_population.extend(copy.deepcopy(c) for c in self.population[:self.elite_count])
        
        # Generate offspring
        while len(new_population) < self.population_size:
            parent1 = self._tournament_select()
            parent2 = self._tournament_select()
            
            offspring1, offspring2 = self._crossover(parent1, parent2)
            
            offspring1 = self._mutate(offspring1)
            offspring2 = self._mutate(offspring2)
            
            new_population.extend([offspring1, offspring2])
        
        # Trim to population size
        self.population = new_population[:self.population_size]
        self.generation += 1
    
    def run(self, generations: int, param_spaces: Dict[str, Dict[str, float]]) -> Chromosome:
        """Run evolution for specified number of generations."""
        if not self.population:
            self.initialize_population(param_spaces)
        
        for _ in range(generations):
            self.evolve()
        
        logger.info(f"Evolution complete. Best fitness: {self.best_chromosome.fitness:.4f}")
        return self.best_chromosome
    
    def save_state(self, filepath: str) -> None:
        """Save evolution state to file."""
        state = {
            'generation': self.generation,
            'best_chromosome': self.best_chromosome.to_dict() if self.best_chromosome else None,
            'history': self.history
        }
        with open(filepath, 'w') as f:
            json.dump(state, f, indent=2)
        logger.info(f"Saved state to {filepath}")
    
    def load_state(self, filepath: str) -> None:
        """Load evolution state from file."""
        with open(filepath, 'r') as f:
            state = json.load(f)
        
        self.generation = state['generation']
        self.history = state['history']
        
        if state['best_chromosome']:
            self.best_chromosome = Chromosome.from_dict(state['best_chromosome'])
        
        logger.info(f"Loaded state from {filepath}")


def get_default_param_spaces() -> Dict[str, Dict[str, float]]:
    """Default parameter spaces for common trading strategies."""
    return {
        'rsi_period': {'min': 5, 'max': 30, 'scale': 0.15},
        'atr_multiplier': {'min': 1.0, 'max': 4.0, 'scale': 0.2},
        'position_size': {'min': 0.1, 'max': 1.0, 'scale': 0.1},
        'stop_loss_atr': {'min': 1.0, 'max': 3.0, 'scale': 0.15},
        'take_profit_atr': {'min': 1.5, 'max': 5.0, 'scale': 0.2}
    }


# Demo function
def run_demo():
    """Demonstrate evolution engine functionality."""
    logger.info("=" * 60)
    logger.info("Kronos Evolution Engine - Demo")
    logger.info("=" * 60)
    
    # Initialize engine
    engine = EvolutionEngine(
        population_size=30,
        elite_count=3,
        mutation_rate=0.15,
        crossover_rate=0.8
    )
    
    # Define parameter spaces
    param_spaces = get_default_param_spaces()
    logger.info(f"Optimizing parameters: {list(param_spaces.keys())}")
    
    # Run evolution
    best = engine.run(generations=20, param_spaces=param_spaces)
    
    # Report results
    logger.info("=" * 60)
    logger.info("Evolution Complete!")
    logger.info(f"Best Fitness: {best.fitness:.4f}")
    logger.info("Best Parameters:")
    for gene in best.genes:
        logger.info(f"  {gene.name}: {gene.value:.4f}")
    logger.info("=" * 60)
    
    # Save state
    state_path = os.path.expanduser("~/kronos/data/evolution_state.json")
    engine.save_state(state_path)
    
    return best


if __name__ == '__main__':
    run_demo()
