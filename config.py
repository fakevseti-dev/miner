"""
Конфигурационный файл для Crypto Miner приложения
Поддерживает разные окружения: development, production, testing
"""

import os
from datetime import timedelta

class Config:
    """Базовая конфигурация"""
    # Flask
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    DEBUG = False
    TESTING = False
    
    # Server
    PORT = int(os.getenv('PORT', 5000))
    HOST = os.getenv('HOST', '127.0.0.1')
    
    # Database
    DATABASE_PATH = os.getenv('DATABASE_PATH', 'data/crypto.db')
    DB_TIMEOUT = 30
    
    # Mining
    MINING_RATE_PER_SECOND = 0.005  # $0.005 за секунду (по умолчанию)
    HASH_RATE_MIN = 40  # TH/s
    HASH_RATE_MAX = 50  # TH/s
    
    # Logging
    LOG_LEVEL = 'INFO'
    LOG_FILE = 'logs/app.log'
    
    # Session
    SESSION_LIFETIME = timedelta(hours=24)
    
    # API
    API_RATE_LIMIT = 100  # requests per minute
    API_TIMEOUT = 30

class DevelopmentConfig(Config):
    """Конфигурация для разработки"""
    DEBUG = True
    TESTING = False
    HOST = '0.0.0.0'
    LOG_LEVEL = 'DEBUG'

class ProductionConfig(Config):
    """Конфигурация для production"""
    DEBUG = False
    TESTING = False
    SECRET_KEY = os.getenv('SECRET_KEY', 'MUST-SET-IN-PRODUCTION')
    HOST = '0.0.0.0'
    LOG_LEVEL = 'WARNING'

class TestingConfig(Config):
    """Конфигурация для тестирования"""
    DEBUG = True
    TESTING = True
    DATABASE_PATH = 'data/test_crypto.db'
    PORT = 5001

# Выбор конфигурации по переменной окружения
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}

def get_config():
    """Получить текущую конфигурацию"""
    env = os.getenv('FLASK_ENV', 'development')
    return config.get(env, config['default'])
