import math
from typing import Optional
import torch
from nltk.tokenize import sent_tokenize
from pydantic import BaseModel
from transformers import (
    AutoModel,
    AutoTokenizer,
    T5ForConditionalGeneration,
    T5Tokenizer,
    DPRContextEncoder,
    DPRQuestionEncoder,
)


# limit transformer batch size to limit parallel inference, otherwise we run
# into memory problems
MAX_BATCH_SIZE = 25  # TODO: take from config
DEFAULT_POOL_METHOD="masked_mean"

class VectorInputConfig(BaseModel):
    pooling_strategy: str


class VectorInput(BaseModel):
    text: str
    config: Optional[VectorInputConfig] = None

class Vectorizer:
    model: AutoModel
    tokenizer: AutoTokenizer
    cuda: bool
    cuda_core: str
    model_type: str

    def __init__(self, model_path: str, cuda_support: bool, cuda_core: str, cuda_memory_pct: float, model_type: str, architecture: str):
        self.cuda = cuda_support
        self.cuda_core = cuda_core
        self.cuda_memory_pct = cuda_memory_pct
        self.model_type = model_type

        self.model_delegate: HFModel = ModelFactory.model(model_type, architecture)
        self.model = self.model_delegate.create_model(model_path)

        if self.cuda:
            self.model.to(self.cuda_core)
            if self.cuda_memory_pct:
                torch.cuda.set_per_process_memory_fraction(self.cuda_memory_pct)
        self.model.eval() # make sure we're in inference mode, not training

        self.tokenizer = self.model_delegate.create_tokenizer(model_path)

    def tokenize(self, text:str):
        return self.tokenizer(text, padding=True, truncation=True, max_length=500, 
                add_special_tokens = True, return_tensors="pt")

    def get_embeddings(self, batch_results):
        return self.model_delegate.get_embeddings(batch_results)

    def get_batch_results(self, tokens, text):
        return self.model_delegate.get_batch_results(tokens, text)

    def pool_embedding(self, batch_results, tokens, config):
        return self.model_delegate.pool_embedding(batch_results, tokens, config)

    async def vectorize(self, text: str, config: VectorInputConfig):
        with torch.no_grad():
            sentences = sent_tokenize(' '.join(text.split(),))
            num_sentences = len(sentences)
            number_of_batch_vectors = math.ceil(num_sentences / MAX_BATCH_SIZE)
            batch_sum_vectors = 0
            for i in range(0, number_of_batch_vectors):
                start_index = i * MAX_BATCH_SIZE
                end_index = start_index + MAX_BATCH_SIZE

                tokens = self.tokenize(sentences[start_index:end_index])
                if self.cuda:
                    tokens.to(self.cuda_core)
                batch_results = self.get_batch_results(tokens, sentences[start_index:end_index])
                batch_sum_vectors += self.pool_embedding(batch_results, tokens, config)
            return batch_sum_vectors.detach() / num_sentences


class HFModel:

    def __init__(self):
        super().__init__()
        self.model = None
        self.tokenizer = None

    def create_tokenizer(self, model_path):
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        return self.tokenizer

    def create_model(self, model_path):
        self.model = AutoModel.from_pretrained(model_path)
        return self.model

    def get_embeddings(self, batch_results):
        return batch_results[0]

    def get_batch_results(self, tokens, text):
        return self.model(**tokens)

    def pool_embedding(self, batch_results, tokens, config: VectorInputConfig):
        pooling_method = self.pool_method_from_config(config)
        if pooling_method == "cls":
            return self.get_embeddings(batch_results)[:, 0, :].sum(0)
        elif pooling_method == "masked_mean":
            return self.pool_sum(self.get_embeddings(batch_results), tokens['attention_mask'])
        else:
            raise Exception(f"invalid pooling method '{pooling_method}'")

    def pool_method_from_config(self, config: VectorInputConfig):
        if config is None:
            return DEFAULT_POOL_METHOD

        if config.pooling_strategy is None or config.pooling_strategy == "":
            return DEFAULT_POOL_METHOD

        return config.pooling_strategy

    def pool_sum(self, embeddings, attention_mask):
        input_mask_expanded = attention_mask.unsqueeze(-1).expand(embeddings.size()).float()
        sum_embeddings = torch.sum(embeddings * input_mask_expanded, 1)
        sum_mask = torch.clamp(input_mask_expanded.sum(1), min=1e-9)
        sentences = sum_embeddings / sum_mask
        return sentences.sum(0)


class DPRModel(HFModel):

    def __init__(self, architecture: str):
        super().__init__()
        self.model = None
        self.architecture = architecture

    def create_model(self, model_path):
        if self.architecture == "DPRQuestionEncoder":
            self.model = DPRQuestionEncoder.from_pretrained(model_path)
        else:
            self.model = DPRContextEncoder.from_pretrained(model_path)
        return self.model

    def get_batch_results(self, tokens, text):
        return self.model(tokens['input_ids'], tokens['attention_mask'])

    def pool_embedding(self, batch_results, tokens, config: VectorInputConfig):
        # no pooling needed for DPR
        return batch_results["pooler_output"][0]


class T5Model(HFModel):

    def __init__(self):
        super().__init__()
        self.model = None
        self.tokenizer = None

    def create_model(self, model_path):
        self.model = T5ForConditionalGeneration.from_pretrained(model_path)
        return self.model

    def create_tokenizer(self, model_path):
        self.tokenizer = T5Tokenizer.from_pretrained(model_path)
        return self.tokenizer

    def get_embeddings(self, batch_results):
        return batch_results["encoder_last_hidden_state"]

    def get_batch_results(self, tokens, text):
        input_ids, attention_mask = tokens['input_ids'], tokens['attention_mask']

        target_encoding = self.tokenizer(
            text, padding="longest", max_length=500, truncation=True
        )
        labels = target_encoding.input_ids
        labels = torch.tensor(labels)

        return self.model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)


class ModelFactory:

    @staticmethod
    def model(model_type, architecture):
        if model_type == 't5':
            return T5Model()
        elif model_type == 'dpr':
            return DPRModel(architecture)
        else:
            return HFModel()
