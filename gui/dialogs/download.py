import io
import os
import wx
import time
import shutil
import struct
import urllib
import random
import wxasync
import tempfile
import traceback
import simplejson
from wx.lib.agw import floatspin
from dataclasses import dataclass
from asyncio.locks import Event

from sd_model_manager.utils.common import try_load_image
from gui import ids, utils
from gui.api import ComfyAPI, ModelManagerAPI
from gui.utils import PROGRAM_ROOT, combine_tag_freq
from gui.comfy_executor import ComfyExecutor
from gui.image_panel import ImagePanel
from gui.async_utils import AsyncShowDialogModal, on_close

CHECKPOINTS_DIR = r"D:\sd\ComfyUI\models\checkpoints"
VAES_DIR = r"D:\sd\ComfyUI\models\vaes"
LORAS_DIR = r"D:\sd\ComfyUI\models\loras"
DEFAULT_POSITIVE_PROMPT = "male, canine, canis, domestic dog, fur, gym"
DEFAULT_NEGATIVE_PROMPT = "(female:1.4), (deformed, distorted, disfigured:1.3), poorly drawn, extra limb, missing limb, floating limbs, (mutated hands and fingers:1.4), disconnected limbs, mutation, mutated, ugly, disgusting, blurry, amputation, no arms, text, watermark, (worst quality:1.4), (low quality:1.4), (monochrome:1.1), bad_quality, watermark, font, text, username, poorly drawn eyes, blurry eyes, (pussy:1.1), vagina, feral,"


def load_prompt(name):
    with open(
        os.path.join(PROGRAM_ROOT, "gui/prompts", name), "r", encoding="utf-8"
    ) as f:
        return simplejson.load(f)


@dataclass
class GeneratePreviewsOptions:
    prompt_before: str
    prompt_after: str
    n_tags: int
    seed: int
    denoise: float


@dataclass
class PreviewPromptData:
    seed: int
    denoise: float
    checkpoint: str
    vae: str
    positive: str
    negative: str
    lora_name: str

    def to_prompt(self):
        prompt = load_prompt("default.json")
        keys_to_attributes = {
            "seed": self.seed, 
            "ckpt_name": self.checkpoint, 
            "vae_name": self.vae, 
        }
        
        

        for key, section in prompt.items():
            if "inputs" in section:
                for input_key, attribute in keys_to_attributes.items():
                    if input_key in section["inputs"]:
                        section["inputs"][input_key] = attribute
        
        prompt["23"]["inputs"]["switch_1"] ="On"
        prompt["23"]["inputs"]["lora_name_1"] = self.lora_name
        if self.lora_name == "add_detail.safetensors":
            prompt["23"]["inputs"]["switch_1"] = "Off"
        return prompt

    def to_hr_prompt(self, image):
        prompt = load_prompt("default.json")
        keys_to_attributes = {
            "seed": self.seed, 
            "ckpt_name": self.checkpoint, 
            "vae_name": self.vae, 
        }

        for key, section in prompt.items():
            if "inputs" in section:
                for input_key, attribute in keys_to_attributes.items():
                    if input_key in section["inputs"]:
                        section["inputs"][input_key] = attribute
        prompt["10"]["inputs"]["lora_name"] = self.lora_name
        return prompt


class CancelException(Exception):
    pass


def find_files(directory, extension):
    if not os.path.exists(directory):
        raise FileNotFoundError(f"The directory {directory} does not exist.")
    files = [file for file in os.listdir(directory)
             if os.path.isfile(os.path.join(directory, file)) and file.endswith(extension)]
    return files


def find_preview_images(basepath):
    i = 0
    images = []
    path = basepath + ".png"
    if os.path.isfile(path):
        images.append(path)

    while True:
        if i == 0:
            path = f"{basepath}.preview.png"
        else:
            path = f"{basepath}.preview.{i}.png"
        if not os.path.isfile(path):
            break
        images.append(path)
        i += 1

    return images


class PreviewGeneratorDialog(wx.Dialog):
    def __init__(self, parent, app, items, duplicate_op):
        super(PreviewGeneratorDialog, self).__init__(
            parent, -1, "Preview Generator", size=app.FromDIP(700, 500)
        )
        self.app = app
        self.comfy_api = ComfyAPI()
        self.duplicate_op = duplicate_op  # "replace", "append"

        self.items = items
        self.result = None
        self.last_data = None
        self.last_output = None
        self.executing_node_id = None
        self.upscaled = False
        self.last_seed = 0
        self.last_upscale_seed = 0
        self.node_text = ""

        utils.set_icons(self)

        positive = DEFAULT_POSITIVE_PROMPT
        self.autogen = False

        # Parameter controls
        self.text_prompt_before = wx.TextCtrl(
            self, 
            id=wx.ID_ANY, 
            value=positive, 
            size=self.Parent.FromDIP(wx.Size(250, 100)), 
            style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER, 
        )
        self.text_prompt_after = wx.TextCtrl(
            self, 
            id=wx.ID_ANY, 
            value=DEFAULT_NEGATIVE_PROMPT, 
            size=self.Parent.FromDIP(wx.Size(250, 100)), 
            style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER, 
        )

        # self.label_n_tags = wx.StaticText(self, wx.ID_ANY, label="# Top Tags")
        # self.spinner_n_tags = wx.SpinCtrl(
        #     self, 
        #     id=wx.ID_ANY, 
        #     value="", 
        #     style=wx.SP_ARROW_KEYS, 
        #     min=-1, 
        #     max=100, 
        #     initial=10, 
        # )

        # tag_totals = combine_tag_freq(items[0].get("tag_frequency") or {})
        # if len(tag_totals) == 0:
        #     self.spinner_n_tags.Disable()
        #     self.spinner_n_tags.SetValue(0)

        self.spinner_seed = wx.SpinCtrl(
            self, 
            id=wx.ID_ANY, 
            value="", 
            style=wx.SP_ARROW_KEYS, 
            min=-1, 
            max=2**16, 
            initial=-1, 
            size=self.Parent.FromDIP(wx.Size(100, 25)), 
        )

        self.spinner_denoise = floatspin.FloatSpin(
            self, 
            id=wx.ID_ANY, 
            min_val=0, 
            max_val=1, 
            increment=0.01, 
            value=0.55, 
            agwStyle=floatspin.FS_LEFT, 
        )
        self.spinner_denoise.SetFormat("%f")
        self.spinner_denoise.SetDigits(2)

        # Status controls/buttons
        self.status_text = wx.StaticText(self, -1, "Ready")
        self.models_text = wx.StaticText(
            self, wx.ID_ANY, label=f"Selected models: {len(self.items)}"
        )
        self.gauge = wx.Gauge(self, -1, 100, size=app.FromDIP(800, 32))
        self.image_panel = ImagePanel(
            self, style=wx.SUNKEN_BORDER, size=app.FromDIP(512, 512)
        )
        self.button_regenerate = wx.Button(self, wx.ID_HELP, "Generate")
        # self.button_regenerate.Disable()
        self.button_upscale = wx.Button(self, wx.ID_APPLY, "Upscale")
        self.button_upscale.Disable()
        self.button_cancel = wx.Button(self, wx.ID_CANCEL, "Cancel")
        self.button_ok = wx.Button(self, wx.ID_OK, "OK")
        self.button_ok.Disable()

        self.Bind(wx.EVT_BUTTON, self.OnRegenerate, id=wx.ID_HELP)
        self.Bind(wx.EVT_BUTTON, self.OnUpscale, id=wx.ID_APPLY)
        self.Bind(wx.EVT_BUTTON, self.OnCancel, id=wx.ID_CANCEL)
        # self.text_prompt_before.Bind(wx.EVT_TEXT_ENTER, self.OnRegenerate)
        # self.text_prompt_after.Bind(wx.EVT_TEXT_ENTER, self.OnRegenerate)
        wxasync.AsyncBind(wx.EVT_BUTTON, self.OnOK, 
                          self.button_ok, id=wx.ID_OK)
        wxasync.AsyncBind(wx.EVT_CLOSE, self.OnClose, self)

        sizerB = wx.StdDialogButtonSizer()
        sizerB.AddButton(self.button_regenerate)
        sizerB.AddButton(self.button_upscale)
        sizerB.AddButton(self.button_cancel)
        sizerB.AddButton(self.button_ok)
        sizerB.Realize()

        sizerLeft = wx.BoxSizer(wx.VERTICAL)
        sizerLeft.Add(self.image_panel)
        sizerLeft.AddSpacer(8)
        sizerLeft.Add(self.status_text)

        # sizerRightMid = wx.BoxSizer(wx.HORIZONTAL)
        # sizerRightMid.Add(
        #     self.label_n_tags, 
        #     proportion=1, 
        #     border=5, 
        #     flag=wx.ALL, 
        # )
        # sizerRightMid.Add(self.spinner_n_tags, proportion=1, flag=wx.ALL, border=5)

        sizerRightAfter = wx.BoxSizer(wx.HORIZONTAL)
        sizerRightAfter.Add(
            wx.StaticText(self, wx.ID_ANY, label="Seed"), 
            proportion=0, 
            border=5, 
            flag=wx.ALL, 
        )
        sizerRightAfter.Add(self.spinner_seed, proportion=1, 
                            flag=wx.ALL, border=5)
        sizerRightAfter.Add(
            wx.StaticText(self, wx.ID_ANY, label="Denoise"), 
            proportion=0, 
            border=5, 
            flag=wx.ALL, 
        )
        sizerRightAfter.Add(self.spinner_denoise, 
                            proportion=1, flag=wx.ALL, border=5)

        sizerRight = wx.StaticBoxSizer(wx.VERTICAL, self, label="Parameters")
        sizerRight.Add(wx.StaticText(self, wx.ID_ANY, label="Positive"))
        sizerRight.Add(self.text_prompt_before, 
                       proportion=2, flag=wx.ALL | wx.EXPAND)
        # sizerRight.Add(sizerRightMid, proportion=1, flag=wx.ALL)
        sizerRight.Add(wx.StaticText(self, wx.ID_ANY, label="Negative"))
        sizerRight.Add(self.text_prompt_after, proportion=2, 
                       flag=wx.ALL | wx.EXPAND)
        sizerRight.Add(sizerRightAfter, proportion=1, flag=wx.ALL)
        sizerRight.Add(
            self.models_text, 
            proportion=0, 
            border=5, 
            flag=wx.ALL, 
        )

        sizerMain = wx.FlexGridSizer(1, 2, 10, 10)
        sizerMain.AddMany([(sizerLeft, 1), (sizerRight, 1, wx.EXPAND)])

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(sizerMain, 1, wx.EXPAND)
        sizer.AddSpacer(8)
        sizer.Add(self.gauge, 0, wx.LEFT | wx.RIGHT)
        sizer.AddSpacer(8)
        sizer.Add(sizerB, 0, wx.LEFT | wx.RIGHT)

        wrapper = wx.BoxSizer(wx.VERTICAL)
        wrapper.Add(sizer, 1, wx.EXPAND | wx.ALL, 10)

        self.SetSizerAndFit(wrapper)

        # self.start_prompt()

    async def save_preview_image(self, item, result):
        self.app.SetStatusText("Saving preview...")
        self.status_text.SetLabel("Saving preview...")

        image_data = self.comfy_api.get_image(result["filename"], result["subfolder"], result["type"])
        basepath = os.path.splitext(item["filepath"])[0]
        path = basepath + ".png"

        if os.path.exists(path):
            i = 0
            while os.path.exists(f"{basepath}.preview.{i or ''}.png"):
                i += 1
            path = f"{basepath}.preview.{i}.png" if self.duplicate_op == "append" else shutil.move(path, f"{basepath}.preview.{i}.png")

        try:
            with open(path, "wb") as f:
                f.write(image_data)
        except FileNotFoundError:
            print(f"File {path} not found. Skipping...")
            return

        new_images = [{"filepath": path, "is_autogenerated": True} for path in find_preview_images(basepath)]
        await self.app.api.update_lora(item["id"], {"preview_images": new_images})
        item["preview_images"] = new_images

        try_load_image.cache_clear()
        await self.app.frame.results_panel.refresh_one_item(item)

        self.app.SetStatusText(f"Saved preview to {path}")
        
    async def OnOK(self, evt):
        self.button_regenerate.Disable()
        self.button_upscale.Disable()
        self.button_cancel.Disable()
        self.button_ok.Disable()
        self.text_prompt_before.Disable()
        self.text_prompt_after.Disable()
        self.spinner_seed.Disable()
        self.spinner_denoise.Disable()

        if self.result is not None:
            await self.save_preview_image(self.items[0], self.result)

        if len(self.items) > 1:
            self.status_text.SetLabel(f"Generating previews...")

            upscaled = self.upscaled
            self.autogen = True
            for i, item in enumerate(self.items[1:]):
                filename = os.path.basename(item["filepath"])
                self.status_text.SetLabel(f"Generating preview for {filename}")
                self.models_text.SetLabel(f"Progress: {i}/{len(self.items)-1}")
                self.spinner_seed.SetValue(self.last_seed)
                e = Event()
                thread = self.start_prompt(item, e=e)
                await e.wait()
                if upscaled:
                    self.status_text.SetLabel("Starting upscale...")
                    self.spinner_seed.SetValue(self.last_upscale_seed)
                    e = Event()
                    thread = self.upscale_prompt(item, e=e)
                    await e.wait()
                if self.result is not None:
                    await self.save_preview_image(item, self.result)
        else:
            self.status_text.SetLabel("Done!")

        self.AsyncEndModal(wx.ID_OK)

    def OnCancel(self, evt):
        self.AsyncEndModal(wx.ID_CANCEL)

    async def OnClose(self, evt):
        if self.autogen:
            return
        await on_close(self, evt)

    def OnRegenerate(self, evt):
        self.status_text.SetLabel("Starting...")
        self.start_prompt(self.items[0])

    def OnUpscale(self, evt):
        self.status_text.SetLabel("Starting upscale...")
        self.upscale_prompt(self.items[0])

    def upscale_prompt(self, item, e=None):
        return utils.start_async_thread(self.run_upscale_prompt, item, e)

    def start_prompt(self, item, e=None):
        return utils.start_async_thread(self.run_prompt, item, e)

    async def run_prompt(self, item, e):
        try:
            self.do_execute(item)
        except CancelException:
            pass
        except Exception as ex:
            print(traceback.format_exc())
            if e is None:
                await self.on_fail(ex)
        if e is not None:
            e.set()

    async def run_upscale_prompt(self, item, e):
        try:
            self.do_upscale(item)
        except CancelException:
            pass
        except Exception as ex:
            print(traceback.format_exc())
            if e is None:
                await self.on_fail(ex)
        if e is not None:
            e.set()

    def get_lora_name(self, item):
        item_filename = os.path.basename(item['filepath'])
        matched_lora = self.find_file_recursive(LORAS_DIR, item_filename.lower(), ".safetensors")
        return item_filename if matched_lora else 'add_detail.safetensors'

    def find_file_recursive(self, directory, target_filename, extension):
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.lower().endswith(extension) and target_filename in file.lower():
                    return os.path.join(root, file)
        return None

    def find_checkpoint(self, item):
        checkpoints_dir = r"D:\sd\ComfyUI\models\checkpoints"
        item_filename = os.path.basename(item['filepath'])
        matched_checkpoint = self.find_file_recursive(checkpoints_dir, item_filename.lower(), ".safetensors")
        return item_filename if matched_checkpoint else "coconutFurryMix2_coconut20Av3.safetensors"

    def find_vae(self, item):
        vaes_dir = r"D:\sd\ComfyUI\models\vaes"
        item_filename = os.path.basename(item['filepath'])
        matched_vae = self.find_file_recursive(vaes_dir, item_filename.lower(), ".pt")
        return item_filename if matched_vae else "MoistMixV2.vae.pt"

    def get_tags(self, item, count=None):
        tags = []
        tag_freq = item.get("tag_frequency")
        if tag_freq is not None:
            totals = utils.combine_tag_freq(tag_freq)
            sort = list(
                sorted(totals.items(), key=lambda p: p[1], reverse=True))
            tags = [p[0] for p in sort]
            if count is not None:
                tags = tags[:count]
        return tags

    def get_prompt_options(self):
        return GeneratePreviewsOptions(
            self.text_prompt_before.GetValue(), 
            self.text_prompt_after.GetValue(), 
            20, 
            self.spinner_seed.GetValue(), 
            self.spinner_denoise.GetValue(), 
        )

    def assemble_prompt_data(self, item):
        print(item)
        options = self.get_prompt_options()
        lora_name = self.get_lora_name(item)
        checkpoint = self.find_checkpoint(item)
        vae = "MoistMixV2.vae.pt"
        seed = random.randint(0, 2**16) if options.seed == -1 else options.seed
        denoise = options.denoise
        positive = f"{options.prompt_before}"
        negative = options.prompt_after
        data = PreviewPromptData(
            seed, denoise, checkpoint, vae, positive, negative, lora_name)
        return data

    def enqueue_prompt_and_wait(self, executor, prompt):
        queue_result = executor.enqueue(prompt)
        prompt_id = queue_result["prompt_id"]

        while True:
            wx.Yield()
            if not self:
                # self was destroyed
                return

            msg = executor.get_status()
            if msg:
                if msg["type"] == "json":
                    status = msg["data"]
                    print(status)
                    ty = status["type"]
                    if ty == "executing":
                        data = status["data"]
                        if data["node"] is not None:
                            self.on_msg_executing(prompt, data)
                        else:
                            if data["prompt_id"] == prompt_id:
                                # Execution is done
                                break
                    elif ty == "progress":
                        self.on_msg_progress(status)
                else:
                    msg = io.BytesIO(msg["data"])
                    ty = struct.unpack(">I", msg.read(4))[0]
                    if ty == 1:  # preview image
                        format = struct.unpack(">I", msg.read(4))[0]
                        if format == 2:
                            img_type = wx.BITMAP_TYPE_PNG
                        else:  # 1
                            img_type = wx.BITMAP_TYPE_JPEG
                        image = wx.Image(msg, type=img_type)
                        self.image_panel.LoadBitmap(image.ConvertToBitmap())

        return prompt_id

    def before_execute(self):
        self.result = None
        self.last_data = None
        self.upscaled = False
        # self.image_panel.Clear()
        self.button_regenerate.Disable()
        self.button_upscale.Disable()
        self.button_ok.Disable()

    def after_execute(self):
        if not self.autogen:
            self.button_regenerate.Enable()
            self.button_ok.Enable()
            self.button_upscale.Enable()
            seed = self.last_upscale_seed if self.upscaled else self.last_seed
            self.status_text.SetLabel(f"Finished. (seed: {seed})")

    def get_output_image(self, prompt_id):
        images, files = self.comfy_api.get_images(prompt_id)
        if not images:
            return None, None
        image_datas = []
        image_files = None
        for node_id in images:
            image_datas += images[node_id]
            image_files = files[node_id]
        if not image_datas:
            return None, None
        return wx.Image(io.BytesIO(image_datas[0])), image_files[0]

    def do_execute(self, item):
        self.before_execute()
        self.last_output = None

        with ComfyExecutor() as executor:
            data = self.assemble_prompt_data(item)
            prompt = data.to_prompt()
            prompt_id = self.enqueue_prompt_and_wait(executor, prompt)

        image, image_location = self.get_output_image(prompt_id)
        if image:
            self.image_panel.LoadBitmap(image.ConvertToBitmap())

        self.last_data = data
        self.last_output = image_location
        self.result = image_location
        self.last_seed = data.seed

        self.after_execute()

    def do_upscale(self, item):
        self.before_execute()

        with ComfyExecutor() as executor:
            data = self.assemble_prompt_data(item)
            prompt = data.to_hr_prompt(self.last_output)
            prompt_id = self.enqueue_prompt_and_wait(executor, prompt)

        image, image_location = self.get_output_image(prompt_id)
        if image:
            self.image_panel.LoadBitmap(image.ConvertToBitmap())

        self.last_data = data
        self.result = image_location
        self.last_upscale_seed = data.seed
        self.upscaled = True

        self.after_execute()

    def on_msg_executing(self, prompt, data):
        node_id = data["node"]
        self.executing_node_id = node_id
        class_type = prompt[node_id]["class_type"]
        self.node_text = f"Node: {class_type}"
        self.status_text.SetLabel(self.node_text)
        self.gauge.SetRange(100)
        self.gauge.SetValue(0)

    def on_msg_progress(self, status):
        value = status["data"]["value"]
        max = status["data"]["max"]
        self.status_text.SetLabel(f"{self.node_text} ({value}/{max})")
        self.gauge.SetRange(max)
        self.gauge.SetValue(value)

    async def on_fail(self, ex):
        dialog = wx.MessageDialog(
            self, 
            f"Failed to generate previews:\n{ex}", 
            "Generation Failed", 
            wx.OK | wx.ICON_ERROR, 
        )
        await wxasync.AsyncShowDialogModal(dialog)
        # dialog.Destroy()
        self.AsyncEndModal(wx.ID_CANCEL)


def any_have_previews(items):
    count = 0

    for item in items:
        filepath = item["filepath"]
        basepath = os.path.splitext(filepath)[0]
        path = basepath + ".png"
        if os.path.exists(path):
            count += 1

    return count


async def run(app, items):
    if not items:
        return

    count = any_have_previews(items)
    op = "replace"

    if count > 0:
        op = "append"

    dialog = PreviewGeneratorDialog(app.frame, app, items, op)
    dialog.Center()
    result = await AsyncShowDialogModal(dialog)
    # dialog.Destroy()
