# core/module_base.py

from abc import ABC, abstractmethod

class BaseModule(ABC):
    """┮Τ家舱膀セ钡"""

    @abstractmethod
    def initialize(self):
        """﹍て家舱更家把计单"""
        pass

    @abstractmethod
    def handle(self, data: dict) -> dict:
        """矪瞶戈肚参Α"""
        pass

    def shutdown(self):
        """睦戈方匡龟"""
        pass
