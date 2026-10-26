from source_identity_config import GPU_TYPES as SUPPORTED_DEVICES
import source_identity_config as config


class Backend:
    def run(self, device):
        if device == "unused":
            return device in config.UNUSED_TYPES
        return self.__consume(device)

    def __consume(self, device):
        return device in SUPPORTED_DEVICES and device in config.GPU_TYPES


def api(device):
    return Backend().run(device)
