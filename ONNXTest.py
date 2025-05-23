import onnxruntime as ort

from rembg import remove, new_session
print(ort.get_available_providers())

session= new_session()
