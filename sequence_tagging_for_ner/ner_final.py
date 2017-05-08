import math
import gzip
import paddle.v2 as paddle
import paddle.v2.evaluator as evaluator
import conll03
import itertools

word_dict, label_dict = conll03.get_dict()
word_dict_len = len(word_dict)
label_dict_len = len(label_dict)

word_dim = 50
caps_dim = 5
context_length = 5
hidden_dim = 300

mix_hidden_lr = 1e-3
default_std = 1 / math.sqrt(hidden_dim) / 3.0
emb_para = paddle.attr.Param(
    name='emb', initial_std=math.sqrt(1. / word_dim), is_static=True)
std_0 = paddle.attr.Param(initial_std=0.)
std_default = paddle.attr.Param(initial_std=default_std)


def d_type(size):
    return paddle.data_type.integer_value_sequence(size)


def ner_net():
    word = paddle.layer.data(name='word', type=d_type(word_dict_len))
    #ws = paddle.layer.data(name='ws', type=d_type(num_ws))

    word_embedding = paddle.layer.mixed(
        name='word_embedding',
        size=word_dim,
        input=paddle.layer.table_projection(input=word, param_attr=emb_para))
    #ws_embedding = paddle.layer.mixed(name='ws_embedding', size=caps_dim, 
    #                    input=paddle.layer.table_projection(input=ws))
    emb_layers = [word_embedding]  #[word_embedding, ws_embedding]

    word_caps_vector = paddle.layer.concat(
        name='word_caps_vector', input=emb_layers)
    hidden_1 = paddle.layer.mixed(
        name='hidden1',
        size=hidden_dim,
        bias_attr=std_default,
        input=[
            paddle.layer.full_matrix_projection(
                input=word_caps_vector, param_attr=std_default)
        ])

    lstm_para_attr = paddle.attr.Param(initial_std=0.0, learning_rate=0.1)
    hidden_para_attr = paddle.attr.Param(
        initial_std=default_std, learning_rate=mix_hidden_lr)

    lstm_1_1 = paddle.layer.lstmemory(
        name='rnn1-1',
        input=hidden_1,
        act=paddle.activation.Relu(),
        gate_act=paddle.activation.Sigmoid(),
        state_act=paddle.activation.Sigmoid(),
        bias_attr=std_0,
        param_attr=lstm_para_attr)
    lstm_1_2 = paddle.layer.lstmemory(
        name='rnn1-2',
        input=hidden_1,
        act=paddle.activation.Relu(),
        gate_act=paddle.activation.Sigmoid(),
        state_act=paddle.activation.Sigmoid(),
        reverse=1,
        bias_attr=std_0,
        param_attr=lstm_para_attr)

    hidden_2_1 = paddle.layer.mixed(
        size=hidden_dim,
        bias_attr=std_default,
        input=[
            paddle.layer.full_matrix_projection(
                input=hidden_1, param_attr=hidden_para_attr),
            paddle.layer.full_matrix_projection(
                input=lstm_1_1, param_attr=lstm_para_attr)
        ])
    hidden_2_2 = paddle.layer.mixed(
        size=hidden_dim,
        bias_attr=std_default,
        input=[
            paddle.layer.full_matrix_projection(
                input=hidden_1, param_attr=hidden_para_attr),
            paddle.layer.full_matrix_projection(
                input=lstm_1_2, param_attr=lstm_para_attr)
        ])

    lstm_2_1 = paddle.layer.lstmemory(
        name='rnn2-1',
        input=hidden_2_1,
        act=paddle.activation.Relu(),
        gate_act=paddle.activation.Sigmoid(),
        state_act=paddle.activation.Sigmoid(),
        reverse=1,
        bias_attr=std_0,
        param_attr=lstm_para_attr)
    lstm_2_2 = paddle.layer.lstmemory(
        name='rnn2-2',
        input=hidden_2_2,
        act=paddle.activation.Relu(),
        gate_act=paddle.activation.Sigmoid(),
        state_act=paddle.activation.Sigmoid(),
        bias_attr=std_0,
        param_attr=lstm_para_attr)

    hidden_3 = paddle.layer.mixed(
        name='hidden3',
        size=hidden_dim,
        bias_attr=std_default,
        input=[
            paddle.layer.full_matrix_projection(
                input=hidden_2_1, param_attr=hidden_para_attr),
            paddle.layer.full_matrix_projection(
                input=lstm_2_1,
                param_attr=lstm_para_attr), paddle.layer.full_matrix_projection(
                    input=hidden_2_2, param_attr=hidden_para_attr),
            paddle.layer.full_matrix_projection(
                input=lstm_2_2, param_attr=lstm_para_attr)
        ])

    output = paddle.layer.mixed(
        name='output',
        size=label_dict_len,
        bias_attr=False,
        input=[
            paddle.layer.full_matrix_projection(
                input=hidden_3, param_attr=std_default)
        ])

    target = paddle.layer.data(name='target', type=d_type(label_dict_len))

    crf_cost = paddle.layer.crf(
        size=label_dict_len,
        input=output,
        label=target,
        param_attr=paddle.attr.Param(
            name='crfw', initial_std=default_std, learning_rate=mix_hidden_lr))

    predict = paddle.layer.crf_decoding(
        size=label_dict_len,
        input=output,
        param_attr=paddle.attr.Param(name='crfw'))

    return output, target, crf_cost, predict


def ner_net_train(data_reader=conll03.train(), num_passes=1):
    # define network topology
    feature_out, target, crf_cost, predict = ner_net()
    crf_dec = paddle.layer.crf_decoding(
        size=label_dict_len,
        input=feature_out,
        label=target,
        param_attr=paddle.attr.Param(name='crfw'))
    evaluator.sum(input=crf_dec)

    # create parameters
    parameters = paddle.parameters.create(crf_cost)
    parameters.set('emb', conll03.get_embedding())

    # create optimizer
    optimizer = paddle.optimizer.Momentum(
        momentum=0,
        learning_rate=2e-4,
        regularization=paddle.optimizer.L2Regularization(rate=8e-4),
        gradient_clipping_threshold=25,
        model_average=paddle.optimizer.ModelAverage(
            average_window=0.5, max_average_window=10000), )

    trainer = paddle.trainer.SGD(
        cost=crf_cost,
        parameters=parameters,
        update_equation=optimizer,
        extra_layers=crf_dec)

    reader = paddle.batch(
        paddle.reader.shuffle(data_reader, buf_size=8192), batch_size=256)

    feeding = {'word': 0, 'target': 1}

    def event_handler(event):
        if isinstance(event, paddle.event.EndIteration):
            if event.batch_id % 100 == 0:
                print "Pass %d, Batch %d, Cost %f, %s" % (
                    event.pass_id, event.batch_id, event.cost, event.metrics)
            if event.batch_id % 1000 == 0:
                result = trainer.test(reader=reader, feeding=feeding)
                print "\nTest with Pass %d, Batch %d, %s" % (
                    event.pass_id, event.batch_id, result.metrics)

        if isinstance(event, paddle.event.EndPass):
            # save parameters
            with gzip.open('params_pass_%d.tar.gz' % event.pass_id, 'w') as f:
                parameters.to_tar(f)

            result = trainer.test(reader=reader, feeding=feeding)
            print "\nTest with Pass %d, %s" % (event.pass_id, result.metrics)

    trainer.train(
        reader=reader,
        event_handler=event_handler,
        num_passes=num_passes,
        feeding=feeding)

    return parameters


def ner_net_infer(parameters=paddle.parameters.Parameters.from_tar(
        gzip.open('ner_params_pass_99.tar.gz')),
                  data_reader=conll03.test()):
    test_creator = data_reader
    test_data = []
    for item in test_creator():
        test_data.append([item[0]])
        if len(test_data) == 10:
            break

    feature_out, target, crf_cost, predict = ner_net()

    lab_ids = paddle.infer(
        output_layer=predict,
        parameters=parameters,
        input=test_data,
        field='id')

    labels_reverse = {}
    for (k, v) in label_dict.items():
        labels_reverse[v] = k
    pre_lab = [labels_reverse[lab_id] for lab_id in lab_ids]
    print pre_lab


if __name__ == '__main__':
    paddle.init(use_gpu=False, trainer_count=1)
    ner_net_train()
    ner_net_infer()
