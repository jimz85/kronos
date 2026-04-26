#!/usr/bin/env python3
"""
Kronos ML Training Script

Trains a simple price predictor using the prepared training data.
Supports sklearn RandomForestRegressor or PyTorch MLP.

Usage:
    python train_predictor.py --coin BTC-USDT --epochs 100 --batch-size 32
"""

import argparse
import os
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Model directory
MODEL_DIR = Path(__file__).parent.parent.parent / 'models' / 'trained'
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# Try to import PyTorch, fall back to sklearn if not available
try:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

try:
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


# Feature columns to use for training
FEATURE_COLS = [
    'rsi', 'adx', 'bollinger_pos', 'macd', 'vol_ratio', 'atr', 'confidence',
    'return_1h', 'volatility'
]

# Label column (default: 1-hour forward return)
DEFAULT_LABEL_COL = 'future_return_1h'


class MLPRegressor(nn.Module):
    """Simple PyTorch MLP for regression."""
    
    def __init__(self, input_dim: int, hidden_dims: list = [64, 32]):
        super().__init__()
        layers = []
        prev_dim = input_dim
        
        for hidden_dim in hidden_dims:
            layers.append(nn.Linear(prev_dim, hidden_dim))
            layers.append(nn.ReLU())
            layers.append(nn.Dropout(0.2))
            prev_dim = hidden_dim
        
        layers.append(nn.Linear(prev_dim, 1))
        self.network = nn.Sequential(*layers)
    
    def forward(self, x):
        return self.network(x).squeeze(-1)


def load_data(coin: str, data_dir: Path = None) -> pd.DataFrame:
    """Load prepared training data from parquet file."""
    if data_dir is None:
        data_dir = Path(__file__).parent.parent / 'data' / 'training'
    
    data_file = data_dir / f"train_{coin.replace('-', '_')}.parquet"
    
    if not data_file.exists():
        raise FileNotFoundError(
            f"Training data not found: {data_file}. "
            f"Please run prepare_training_data.py first."
        )
    
    return pd.read_parquet(data_file)


def prepare_features(df: pd.DataFrame, feature_cols: list = None, label_col: str = DEFAULT_LABEL_COL):
    """Prepare features and labels for training."""
    if feature_cols is None:
        feature_cols = FEATURE_COLS
    
    # Check for required columns
    missing_cols = [c for c in feature_cols if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Missing feature columns: {missing_cols}")
    
    if label_col not in df.columns:
        raise ValueError(f"Missing label column: {label_col}")
    
    # Extract features and labels
    X = df[feature_cols].values
    y = df[label_col].values
    
    # Handle any NaN values
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
    
    return X, y, feature_cols


def train_sklearn_model(X_train, y_train, X_val, y_val, epochs: int = 100):
    """Train sklearn RandomForestRegressor model."""
    if not HAS_SKLEARN:
        raise ImportError("sklearn is required for this mode")
    
    print("Training sklearn RandomForestRegressor...")
    
    model = RandomForestRegressor(
        n_estimators=100,
        max_depth=10,
        min_samples_split=10,
        min_samples_leaf=5,
        random_state=42,
        n_jobs=-1
    )
    
    # Train with early stopping using validation set
    best_score = float('-inf')
    best_model = model
    
    for i in range(epochs):
        model.fit(X_train, y_train)
        
        train_score = model.score(X_train, y_train)
        val_score = model.score(X_val, y_val)
        
        if val_score > best_score:
            best_score = val_score
            best_model = model
        
        if (i + 1) % 10 == 0:
            print(f"  Epoch {i+1}/{epochs} - Train R2: {train_score:.4f}, Val R2: {val_score:.4f}")
    
    print(f"Best validation R2: {best_score:.4f}")
    return best_model


def train_pytorch_model(X_train, y_train, X_val, y_val, epochs: int = 100, batch_size: int = 32):
    """Train PyTorch MLP model."""
    if not HAS_TORCH:
        raise ImportError("PyTorch is required for this mode")
    
    print(f"Training PyTorch MLP (epochs={epochs}, batch_size={batch_size})...")
    
    # Scale features
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_val_scaled = scaler.transform(X_val)
    
    # Convert to tensors
    X_train_t = torch.FloatTensor(X_train_scaled)
    y_train_t = torch.FloatTensor(y_train)
    X_val_t = torch.FloatTensor(X_val_scaled)
    y_val_t = torch.FloatTensor(y_val)
    
    # Create data loaders
    train_dataset = TensorDataset(X_train_t, y_train_t)
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    
    # Initialize model
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = MLPRegressor(input_dim=X_train.shape[1]).to(device)
    
    criterion = nn.MSELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min', patience=10)
    
    best_val_loss = float('inf')
    best_model_state = None
    
    for epoch in range(epochs):
        model.train()
        train_loss = 0.0
        
        for batch_X, batch_y in train_loader:
            batch_X, batch_y = batch_X.to(device), batch_y.to(device)
            
            optimizer.zero_grad()
            outputs = model(batch_X)
            loss = criterion(outputs, batch_y)
            loss.backward()
            optimizer.step()
            
            train_loss += loss.item()
        
        # Validation
        model.eval()
        with torch.no_grad():
            val_outputs = model(X_val_t.to(device))
            val_loss = criterion(val_outputs, y_val_t.to(device)).item()
        
        scheduler.step(val_loss)
        
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_model_state = model.state_dict().copy()
        
        if (epoch + 1) % 10 == 0:
            train_loss_avg = train_loss / len(train_loader)
            print(f"  Epoch {epoch+1}/{epochs} - Train Loss: {train_loss_avg:.4f}, Val Loss: {val_loss:.4f}")
    
    # Load best model
    model.load_state_dict(best_model_state)
    print(f"Best validation loss: {best_val_loss:.4f}")
    
    return model, scaler


def main():
    parser = argparse.ArgumentParser(description='Train Kronos price predictor')
    parser.add_argument('--coin', type=str, default='BTC-USDT',
                        help='Trading pair symbol (e.g., BTC-USDT)')
    parser.add_argument('--epochs', type=int, default=100,
                        help='Number of training epochs')
    parser.add_argument('--batch-size', type=int, default=32,
                        help='Batch size for PyTorch training')
    parser.add_argument('--model-type', type=str, default='sklearn',
                        choices=['sklearn', 'pytorch'],
                        help='Model type to use for training')
    parser.add_argument('--label-col', type=str, default=DEFAULT_LABEL_COL,
                        help='Label column to predict')
    
    args = parser.parse_args()
    
    print(f"Kronos ML Training")
    print(f"=" * 50)
    print(f"Coin: {args.coin}")
    print(f"Epochs: {args.epochs}")
    print(f"Batch size: {args.batch_size}")
    print(f"Model type: {args.model_type}")
    print(f"Label column: {args.label_col}")
    print()
    
    # Check dependencies
    if args.model_type == 'pytorch' and not HAS_TORCH:
        print("ERROR: PyTorch not available. Falling back to sklearn.")
        args.model_type = 'sklearn'
    
    if args.model_type == 'sklearn' and not HAS_SKLEARN:
        print("ERROR: sklearn not available. Please install scikit-learn.")
        sys.exit(1)
    
    # Step 1: Load data
    print("Step 1: Loading training data...")
    df = load_data(args.coin)
    print(f"  Loaded {len(df)} samples")
    
    # Step 2: Prepare features
    print("Step 2: Preparing features...")
    X, y, feature_cols = prepare_features(df, label_col=args.label_col)
    print(f"  Features shape: {X.shape}")
    print(f"  Labels shape: {y.shape}")
    print(f"  Feature columns: {feature_cols}")
    
    # Step 3: Train/val split (80/20)
    print("Step 3: Splitting data (80/20)...")
    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.2, random_state=42
    )
    print(f"  Training samples: {len(X_train)}")
    print(f"  Validation samples: {len(X_val)}")
    
    # Step 4: Train model
    print("Step 4: Training model...")
    if args.model_type == 'sklearn':
        model = train_sklearn_model(X_train, y_train, X_val, y_val, epochs=args.epochs)
        model_info = {
            'type': 'sklearn',
            'model': model,
            'feature_cols': feature_cols,
            'label_col': args.label_col
        }
    else:
        model, scaler = train_pytorch_model(
            X_train, y_train, X_val, y_val,
            epochs=args.epochs,
            batch_size=args.batch_size
        )
        model_info = {
            'type': 'pytorch',
            'model': model,
            'scaler': scaler,
            'feature_cols': feature_cols,
            'label_col': args.label_col,
            'input_dim': X.shape[1]
        }
    
    # Step 5: Save model
    print("Step 5: Saving model...")
    model_path = MODEL_DIR / f"predictor_{args.coin.replace('-', '_')}.pkl"
    
    with open(model_path, 'wb') as f:
        pickle.dump(model_info, f)
    
    print(f"  Model saved to: {model_path}")
    
    # Summary
    print()
    print("Training Complete!")
    print(f"  Coin: {args.coin}")
    print(f"  Model type: {args.model_type}")
    print(f"  Model saved: {model_path}")


if __name__ == '__main__':
    main()
