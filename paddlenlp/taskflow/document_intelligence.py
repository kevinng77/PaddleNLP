# Copyright (c) 2022  PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import collections
import paddle
from ..transformers import AutoTokenizer
from .utils import download_file, ImageReader, get_doc_pred, find_answer_pos
from .task import Task

usage = r"""
            from paddlenlp import Taskflow
            docprompt = Taskflow("document_intelligence")
            # Types of doc: A string containing a local path to an image
            docprompt({"doc": "./invoice.jpg", "prompt": ["发票号码是多少?", "校验码是多少?"]})
            # Types of doc: A string containing a http link pointing to an image
            docprompt({"doc": "https://bj.bcebos.com/paddlenlp/taskflow/document_intelligence/images/invoice.jpg", "prompt": ["发票号码是多少?", "校验码是多少?"]})
            '''
            [{'prompt': '发票号码是多少?', 'result': [{'value': 'No44527206', 'prob': 0.96, 'start': 7, 'end': 10}]}, {'prompt': '校验码是多少?', 'result': [{'value': '01107 555427109891646', 'prob': 1.0, 'start': 263, 'end': 271}]}]
            '''

            # Batch input
            batch_input = [
                {"doc": "./invoice.jpg", "prompt": ["发票号码是多少?", "校验码是多少?"]},
                {"doc": "./resume.png", "prompt": ["五百丁本次想要担任的是什么职位?", "五百丁是在哪里上的大学?", "大学学的是什么专业?"]}
            ]
            docprompt(batch_input)
            '''
            [[{'prompt': '发票号码是多少?', 'result': [{'value': 'No44527206', 'prob': 0.96, 'start': 7, 'end': 10}]}, {'prompt': '校验码是多少?', 'result': [{'value': '01107 555427109891646', 'prob': 1.0, 'start': 263, 'end': 271}]}], [{'prompt': '五百丁本次想要担任的是什么职位?', 'result': [{'value': '客户经理', 'prob': 1.0, 'start': 180, 'end': 183}]}, {'prompt': '五百丁是在哪里上的大学?', 'result': [{'value': '广州五百丁学院', 'prob': 1.0, 'start': 32, 'end': 38}]}, {'prompt': '大学学的是什么专业?', 'result': [{'value': '金融学(本科）', 'prob': 0.74, 'start': 39, 'end': 45}]}]]
            '''
         """

URLS = {
    "docprompt": [
        "https://bj.bcebos.com/paddlenlp/taskflow/document_intelligence/docprompt/docprompt_params.tar",
        "8eae8148981731f230b328076c5a08bf"
    ],
}


class DocPromptTask(Task):
    """
    The document intelligence model, give the querys and predict the answers. 
    Args:
        task(string): The name of task.
        model(string): The model name in the task.
        kwargs (dict, optional): Additional keyword arguments passed along to the specific task. 
    """

    def __init__(self, task, model, **kwargs):
        super().__init__(task=task, model=model, **kwargs)
        try:
            from paddleocr import PaddleOCR
        except:
            raise ImportError(
                "Please install the dependencies first, pip install paddleocr --upgrade"
            )
        self._batch_size = kwargs.get("batch_size", 1)
        self._topn = kwargs.get("topn", 1)
        self._lang = kwargs.get("lang", "ch")
        self._use_gpu = False if paddle.get_device() == 'cpu' else True
        self._ocr = PaddleOCR(use_angle_cls=True,
                              show_log=False,
                              use_gpu=self._use_gpu,
                              lang=self._lang)
        self._usage = usage
        download_file(self._task_path, "docprompt_params.tar",
                      URLS[self.model][0], URLS[self.model][1])
        self._get_inference_model()
        self._construct_tokenizer()
        self._reader = ImageReader(super_rel_pos=False,
                                   tokenizer=self._tokenizer)

    def _construct_tokenizer(self):
        """
        Construct the tokenizer for the predictor.
        """
        self._tokenizer = AutoTokenizer.from_pretrained(
            "ernie-layoutx-base-uncased")

    def _preprocess(self, inputs):
        """
        Transform the raw text to the model inputs, two steps involved:
           1) Transform the raw text to token ids.
           2) Generate the other model inputs from the raw text and token ids.
        """
        preprocess_results = self._check_input_text(inputs)
        for example in preprocess_results:
            if "word_boxes" in example.keys():
                ocr_result = example["word_boxes"]
                example["ocr_type"] = "word_boxes"
            else:
                ocr_result = self._ocr.ocr(example["doc"], cls=True)
                example["ocr_type"] = "ppocr"
            example["ocr_result"] = ocr_result
        return preprocess_results

    def _run_model(self, inputs):
        """
        Run the task model from the outputs of the `_tokenize` function. 
        """
        all_predictions_list = []
        for example in inputs:
            ocr_result = example["ocr_result"]
            doc_path = example["doc"]
            prompt = example["prompt"]
            ocr_type = example["ocr_type"]

            if not ocr_result:
                all_predictions = [{
                    "prompt":
                    p,
                    "result": [{
                        'value': '',
                        'prob': 0.0,
                        'start': -1,
                        'end': -1
                    }]
                } for p in prompt]
            else:
                data_loader = self._reader.data_generator(
                    ocr_result, doc_path, prompt, self._batch_size, ocr_type)

                RawResult = collections.namedtuple("RawResult",
                                                   ["unique_id", "seq_logits"])

                all_results = []
                for data in data_loader:
                    for idx in range(len(self.input_names)):
                        self.input_handles[idx].copy_from_cpu(data[idx])
                    self.predictor.run()
                    outputs = [
                        output_handle.copy_to_cpu()
                        for output_handle in self.output_handle
                    ]
                    unique_ids, seq_logits = outputs

                    for idx in range(len(unique_ids)):
                        all_results.append(
                            RawResult(
                                unique_id=int(unique_ids[idx]),
                                seq_logits=seq_logits[idx],
                            ))

                all_examples = self._reader.examples["infer"]
                all_features = self._reader.features["infer"]
                all_key_probs = [1 for _ in all_examples]

                example_index_to_features = collections.defaultdict(list)

                for feature in all_features:
                    example_index_to_features[feature.qas_id].append(feature)

                unique_id_to_result = {}
                for result in all_results:
                    unique_id_to_result[result.unique_id] = result

                all_predictions = []

                for (example_index, example) in enumerate(all_examples):
                    example_qas_id = example.qas_id
                    example_query = example.keys[0]
                    features = example_index_to_features[example_qas_id]

                    preds = []
                    # keep track of the minimum score of null start+end of position 0
                    for feature in features:
                        if feature.unique_id not in unique_id_to_result:
                            continue
                        result = unique_id_to_result[feature.unique_id]

                        # find preds
                        ans_pos = find_answer_pos(result.seq_logits, feature)
                        preds.extend(
                            get_doc_pred(result, ans_pos, example,
                                         self._tokenizer, feature, True,
                                         all_key_probs, example_index))

                    if not preds:
                        preds.append({
                            'value': '',
                            'prob': 0.,
                            'start': -1,
                            'end': -1
                        })
                    else:
                        preds = sorted(
                            preds, key=lambda x: x["prob"])[::-1][:self._topn]
                    all_predictions.append({
                        "prompt": example_query,
                        "result": preds
                    })
            all_predictions_list.append(all_predictions)
        return all_predictions_list

    def _postprocess(self, inputs):
        """
        The model output is tag ids, this function will convert the model output to raw text.
        """
        results = inputs
        results = results[0] if len(results) == 1 else results
        return results

    def _check_input_text(self, inputs):
        inputs = inputs[0]
        if isinstance(inputs, dict):
            inputs = [inputs]
        if isinstance(inputs, list):
            input_list = []
            for example in inputs:
                data = {}
                if isinstance(example, dict):
                    if "doc" not in example.keys():
                        raise ValueError(
                            "Invalid inputs, the inputs should contain an url to an image or a local path."
                        )
                    else:
                        if isinstance(example["doc"], str):
                            if example["doc"].startswith("http://") or example[
                                    "doc"].startswith("https://"):
                                download_file("./",
                                              example["doc"].rsplit("/", 1)[-1],
                                              example["doc"])
                                doc_path = example["doc"].rsplit("/", 1)[-1]
                            else:
                                doc_path = example["doc"]
                            data["doc"] = doc_path
                        else:
                            raise ValueError(
                                f"Incorrect path or url, URLs must start with `http://` or `https://`"
                            )
                    if "prompt" not in example.keys():
                        raise ValueError(
                            "Invalid inputs, the inputs should contain the prompt."
                        )
                    else:
                        if isinstance(example["prompt"], list) and all(
                                isinstance(s, str) for s in example["prompt"]):
                            data["prompt"] = example["prompt"]
                        else:
                            raise TypeError(
                                "Incorrect prompt, prompt should be list of string."
                            )
                    if "word_boxes" in example.keys():
                        data["word_boxes"] = example["word_boxes"]
                    input_list.append(data)
                else:
                    raise TypeError(
                        "Invalid inputs, input for document intelligence task should be dict or list of dict, but type of {} found!"
                        .format(type(example)))
        else:
            raise TypeError(
                "Invalid inputs, input for document intelligence task should be dict or list of dict, but type of {} found!"
                .format(type(inputs)))
        return input_list

    def _construct_model(self, model):
        """
        Construct the inference model for the predictor.
        """
        pass

    def _construct_input_spec(self):
        """
        Construct the input spec for the convert dygraph model to static model.
        """
        pass
