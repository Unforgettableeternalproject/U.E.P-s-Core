# core/module_base.py

from abc import ABC, abstractmethod

class BaseModule(ABC):
    """�Ҧ��Ҳժ��򥻱��f"""

    @abstractmethod
    def initialize(self):
        """��l�ƼҲաA�p���J�ҫ��B�ѼƵ�"""
        pass

    @abstractmethod
    def handle(self, data: dict) -> dict:
        """�B�z��ƨæ^�ǲΤ@�榡"""
        pass

    def shutdown(self):
        """����귽�A�i���@"""
        pass
