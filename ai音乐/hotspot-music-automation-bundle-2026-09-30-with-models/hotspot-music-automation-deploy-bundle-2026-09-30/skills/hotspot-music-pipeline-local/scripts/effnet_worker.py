from __future__ import annotations
import argparse, json
from pathlib import Path

def main():
    p = argparse.ArgumentParser(); p.add_argument('--audio', required=True); p.add_argument('--model', required=True)
    p.add_argument('--classifier'); p.add_argument('--labels'); a = p.parse_args()
    import essentia.standard as es
    import numpy as np
    audio = es.MonoLoader(filename=a.audio, sampleRate=16000)()
    emb = np.asarray(es.TensorflowPredictEffnetDiscogs(graphFilename=a.model, output='PartitionedCall:1')(audio))
    out = {'status':'ok', 'model':a.model, 'model_role':'music_embedding',
                      'embedding_shape':list(emb.shape), 'embedding_mean':emb.mean(axis=0).astype(float).tolist(),
                      'tags_status':'unavailable',
                      'tags_reason':'Discogs-Effnet graph provides embeddings; configure a compatible classifier head and labels file for Discogs genre probabilities',
                      'lyrics_role':'separate_lyrics_module'}
    if a.classifier and a.labels and Path(a.classifier).exists() and Path(a.labels).exists():
        labels = json.loads(Path(a.labels).read_text(encoding='utf-8'))
        if isinstance(labels, dict): labels = labels.get('classes') or labels.get('labels') or list(labels.values())
        pred = np.asarray(es.TensorflowPredict2D(graphFilename=a.classifier, output='model/Softmax')(emb))
        probs = pred.mean(axis=0); order = np.argsort(probs)[::-1][:20]
        out['tags_status'] = 'ok'; out['tags'] = [{'label': str(labels[int(i)]), 'probability': float(probs[int(i)])} for i in order if int(i) < len(labels)]
    print(json.dumps(out, ensure_ascii=False))

if __name__ == '__main__': main()
