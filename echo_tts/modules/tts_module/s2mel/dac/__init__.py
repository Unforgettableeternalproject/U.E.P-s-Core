__version__ = "1.0.0"

# preserved here for legacy reasons
__model_version__ = "latest"

# 註解掉 audiotools 相關代碼,因為 lite_engine 只需要 VectorQuantize 類別
# import audiotools
# audiotools.ml.BaseModel.INTERN += ["dac.**"]
# audiotools.ml.BaseModel.EXTERN += ["einops"]

# 只導入需要的部分
from . import nn
# from . import model
# from . import utils
# from .model import DAC
# from .model import DACFile
